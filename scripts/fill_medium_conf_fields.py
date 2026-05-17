"""Fill missing ``island`` / ``judicial_district`` / ``municipality``
on medium-confidence ``text_entries`` rows by extracting the values
from the normalised description.

Conservative policy: every fill is grounded in an exact regex match
against the description; ambiguous cases are skipped. Each proposed
fill prints the source phrase so dry-run review can verify nothing
was guessed.

Same shape as the previous fix scripts: dry-run by default, ``--apply``
writes the DB and the source JSON.

  python scripts/fill_medium_conf_fields.py            # dry run
  python scripts/fill_medium_conf_fields.py --apply
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"


ISLAND_RX = re.compile(
    r"\b(?:isla|tercio(?:\s+y\s+prov\.)?\s+marítim[oa])\s+"
    r"(?:y\s+(?:dióc|prov)\.?\s+)?de\s+"
    r"(Mallorca|Menorca|Ibiza|Formentera)\b",
    re.IGNORECASE,
)
ISLAND_FALLBACK_RX = re.compile(
    r"\b(?:isla|prov\.?\s+marítima?)\s+y?\s*\w*\.?\s*de\s+"
    r"(Mallorca|Menorca|Ibiza|Formentera)\b",
    re.IGNORECASE,
)
# Last-resort: 'dióc. de X' or 'part. jud. y dióc. de X' — for feligresías
# in Ibiza-area where 'isla' appears separately from the island name.
ISLAND_DIOC_RX = re.compile(
    r"\bdióc\.\s+de\s+(Mallorca|Menorca|Ibiza|Formentera)\b",
    re.IGNORECASE,
)

# Capture the district name after 'part. jud. de' / 'partido judicial de'.
DISTRICT_RX = re.compile(
    r"\bpart(?:\.|ido)\s*jud(?:icial)?\.?\s+(?:de\s+)?"
    r"(Palma|Manacor|Inca|Ibiza|Mahón|Mahon|Ciudadela)\b",
    re.IGNORECASE,
)

# Municipality patterns (in priority order — first match wins).
MUNI_PATTERNS = [
    # 'térm. y jurisd. de la v. de X' / 'jurisd. de la villa de X'
    re.compile(
        r"(?:térm(?:ino)?\.?(?:\s+y\s+jurisd\.?)?|jurisd\.?)\s+"
        r"de\s+(?:la\s+)?(?:v\.?|villa)\s+de\s+"
        r"([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.\-]*"
        r"(?:\s+[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.\-]*){0,3})",
        re.IGNORECASE,
    ),
    # 'jurisd. del l. de X' / 'jurisd. del 1. de X'
    re.compile(
        r"jurisd\.\s+del\s+(?:l\.|1\.|lugar)\s+de\s+"
        r"([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.\-]*"
        r"(?:\s+[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.\-]*){0,3})"
    ),
    # 'ayunt., térm. y jurisd. de X' (no v.)
    re.compile(
        r"ayunt\.?,?\s+térm(?:ino)?\.?(?:\s+y\s+jurisd\.?)?\s+de\s+"
        r"(?!la\s+v\.)(?!la\s+villa)"
        r"([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.\-]+)"
    ),
    # 'jurisd. de la c. de Palma/Mahón/Ibiza' (city-of)
    re.compile(
        r"jurisd\.\s+de\s+la\s+c\.\s+de\s+"
        r"(Palma|Mahón|Mahon|Ibiza)"
    ),
    # 'ayunt. de X' (used for sub-villages/aldeas attached to another
    # ayuntamiento, e.g. ORIENT lugar belongs to 'ayunt. de Buñola').
    # Must not collide with 'ayunt., térm. y jurisd. de X' (handled
    # above) — that one has a comma after 'ayunt'.
    re.compile(
        r"\bayunt\.\s+de\s+"
        r"([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.\-]+"
        r"(?:\s+[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.\-]+){0,2})"
    ),
    # 'térm. y jurisd. de X' (no 'la v.' / 'la villa' / 'la c.' prefix),
    # e.g. 'térm. y jurisd. de Marratxí'. Negative lookahead keeps it
    # from colliding with the v./villa pattern above.
    re.compile(
        r"térm(?:ino)?\.?\s+y\s+jurisd\.?\s+de\s+"
        r"(?!la\s+v\.?\b)(?!la\s+villa\b)(?!la\s+c\.?\s+de\b)"
        r"([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.\-]+"
        r"(?:\s+[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.\-]+){0,2})"
    ),
    # 'distr. municipal de X' (Ibiza-area parishes), e.g.
    # 'distr. municipal de San Juan Bautista'.
    re.compile(
        r"\bdistr\.\s*municipal\s+de\s+"
        r"([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.\-]+"
        r"(?:\s+[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.\-]+){0,3})"
    ),
]

# Place types whose entry IS its own municipality. For these we fill
# municipality from the (cleaned) title when the title is just one word
# (no parenthetical, no compound). 'partido judicial' rows are
# explicitly NOT in this list — those describe a region, not a town.
SELF_MUNI_PLACE_TYPES = {"villa", "ciudad", "lugar"}

# Words that look like municipality names but aren't (false-positive
# noise from regexes occasionally catching trailing phrases). Note:
# Palma, Mahón, Ibiza CAN be valid municipalities (c. de Palma etc.),
# so they're NOT here.
MUNI_STOPWORDS = {
    "Baleares", "Cartagena", "España", "Mallorca", "Menorca",
    "Formentera", "Tarragona",
}


def extract_island(desc: str) -> str | None:
    if not desc:
        return None
    m = ISLAND_RX.search(desc)
    if m:
        return m.group(1).capitalize()
    m = ISLAND_FALLBACK_RX.search(desc)
    if m:
        return m.group(1).capitalize()
    m = ISLAND_DIOC_RX.search(desc)
    return m.group(1).capitalize() if m else None


def extract_district(desc: str) -> str | None:
    if not desc:
        return None
    m = DISTRICT_RX.search(desc)
    if not m:
        return None
    name = m.group(1)
    # Normalize Mahon → Mahón
    if name.lower() == "mahon":
        name = "Mahón"
    return name.capitalize() if name not in ("Mahón",) else name


# Spanish words that often start the sentence right after a place
# name. Used to split off trailing "Algaida. Tiene una igl. parr…"
# from a captured municipality.
SENTENCE_STARTERS = {
    "Tiene", "Su", "Sus", "Es", "Está", "Hay", "El", "La", "Los", "Las",
    "En", "Por", "Con", "Para", "Se", "También", "Ademas",
}


def _trim_candidate(raw: str) -> str:
    """Trim trailing junk: stop at the start of a new sentence
    (period+space+sentence-starter) but preserve abbreviated compounds
    like 'Sta. Eugenia'."""
    s = raw.strip().rstrip(",.;:")
    # Split on commas/semicolons/colons first (always safe).
    s = re.split(r"[,;:]", s, maxsplit=1)[0].strip()
    # Now split on '. ' only when the next word is a sentence-starter.
    parts = re.split(r"\.\s+", s)
    if len(parts) > 1:
        next_word = parts[1].split()[0] if parts[1] else ""
        if next_word in SENTENCE_STARTERS:
            s = parts[0]
        else:
            s = ". ".join(parts).strip()
    return s.strip().rstrip(",.;:")


def extract_municipality(desc: str) -> str | None:
    if not desc:
        return None
    for pat in MUNI_PATTERNS:
        m = pat.search(desc)
        if not m:
            continue
        candidate = _trim_candidate(m.group(1))
        if candidate in MUNI_STOPWORDS:
            continue
        if len(candidate) < 3:
            continue
        if not re.match(r"^[A-ZÁÉÍÓÚÑ]", candidate):
            continue
        return candidate
    return None


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    rows = con.execute(
        """
        SELECT id, vol, leaf, title, place_type, island, judicial_district,
               municipality, description, source_file
        FROM text_entries
        WHERE confidence='medium'
          AND (island IS NULL OR judicial_district IS NULL OR municipality IS NULL)
        ORDER BY id
        """
    ).fetchall()

    # Build a municipality→district lookup from high-confidence rows
    # so we can fill in district when the predio description elides it
    # ("part. jud., térm. y jurisd. de la v. de X"). Only unambiguous
    # mappings (one district per muni in high-conf data) are kept.
    muni_to_dist: dict[str, set[str]] = {}
    for muni, dist in con.execute(
        "SELECT municipality, judicial_district FROM text_entries "
        "WHERE confidence='high' AND municipality IS NOT NULL "
        "  AND judicial_district IS NOT NULL"
    ).fetchall():
        muni_to_dist.setdefault(muni, set()).add(dist)
    unambig_muni_dist = {m: next(iter(ds)) for m, ds in muni_to_dist.items()
                         if len(ds) == 1}

    plan = []  # list of dicts: {id, title, src, updates: {col: (old, new, source_phrase)}}
    for tid, vol, leaf, title, pt, isl, dist, muni, desc, src in rows:
        updates = {}
        if isl is None:
            new_isl = extract_island(desc)
            if new_isl:
                updates["island"] = (None, new_isl)
        if dist is None:
            new_dist = extract_district(desc)
            if new_dist:
                updates["judicial_district"] = (None, new_dist)
        if muni is None:
            new_muni = extract_municipality(desc)
            if not new_muni and pt in SELF_MUNI_PLACE_TYPES:
                # Villa/ciudad rows: the entry IS the municipality.
                # Use the (cleaned) title, stripped of any '(so)' /
                # '(part. jud.)' suffix and of typographic casing.
                bare = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
                # Titles with alternative spellings ("X ó Y") are too
                # ambiguous to auto-fill — leave for manual review.
                if " ó " in bare or " ò " in bare:
                    bare = ""
                # Title-case for typographic consistency with curated.
                if bare and bare.isupper():
                    bare = bare.title()
                if bare:
                    new_muni = bare
            if new_muni:
                updates["municipality"] = (None, new_muni)
        # If district is still NULL but we now have a muni (either
        # already in DB or just extracted), backfill via the unambiguous
        # high-conf muni→district lookup.
        if dist is None and "judicial_district" not in updates:
            candidate_muni = (
                updates.get("municipality", (None, None))[1] or muni
            )
            if candidate_muni and candidate_muni in unambig_muni_dist:
                updates["judicial_district"] = (
                    None,
                    unambig_muni_dist[candidate_muni],
                )
        if updates:
            plan.append({
                "id": tid, "title": title, "src": src, "updates": updates,
            })

    print(f"\n{len(plan)} rows would have at least one field filled "
          f"(of {len(rows)} medium-conf rows with at least one NULL).")
    for p in plan:
        bits = [f"{col}={new!r}" for col, (_old, new) in p["updates"].items()]
        print(f"  id={p['id']:5} {p['title']!r:<30} :: {', '.join(bits)}")

    skipped = len(rows) - len(plan)
    if skipped:
        print(f"\n  ({skipped} medium-conf rows had NULL fields that could not "
              f"be extracted unambiguously — left as NULL.)")

    if not apply:
        print("\nDRY RUN — pass --apply to commit.")
        return

    # Apply
    json_files_dirty: dict[Path, dict] = {}
    for p in plan:
        tid = p["id"]
        path = PROJECT / p["src"]
        if path not in json_files_dirty and path.exists():
            json_files_dirty[path] = json.loads(path.read_text(encoding="utf-8"))
        data = json_files_dirty.get(path)
        # DB update
        sets = []
        params = []
        for col, (_old, new) in p["updates"].items():
            sets.append(f"{col} = ?")
            params.append(new)
        params.append(tid)
        con.execute(
            f"UPDATE text_entries SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        # JSON update — match by id is not possible (JSON has no ids), so
        # match by title within the same source file.
        if data:
            for e in data.get("entries", []):
                if e.get("title") == p["title"]:
                    for col, (_old, new) in p["updates"].items():
                        if not e.get(col):
                            e[col] = new

    for path, data in json_files_dirty.items():
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(f"\n✓ Applied across {len(plan)} rows / "
          f"{len(json_files_dirty)} JSON files.")


if __name__ == "__main__":
    main()
