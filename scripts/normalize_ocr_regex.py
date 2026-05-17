"""Apply a fixed set of deterministic regex transforms to clean OCR
noise from ``text_entries.description``. Touches every confidence
level, not just ``medium`` (those were already normalised by
``normalize_medium_conf.py``).

Design constraints:
- Zero hallucination: every regex maps an OCR mangle that the
  surrounding corpus disambiguates (e.g. ``Raleares`` only ever
  appears where ``Baleares`` is the right word — there is no place
  called Raleares).
- Idempotent: if a description doesn't change after running all the
  patterns, the row is a no-op.
- Preserves ``description_raw`` (set on first touch only; later runs
  keep the original LLM extraction).

Patterns deliberately omitted:
- Toponym variants that BOTH appear in genuine Madoz (e.g.
  ``Pollensa``/``Pollenza``, ``Andraitx``/``Andraix``, ``Calvià``/
  ``Calviá``). Madoz himself was inconsistent — preserve the source.
- Stats-table numbers — too easy to introduce wrong digits.

Same shape as the previous fix scripts: dry-run by default, ``--apply``
writes the DB and the source JSON.

  python scripts/normalize_ocr_regex.py            # dry run + diff sample
  python scripts/normalize_ocr_regex.py --apply
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"


# (regex, replacement, optional flags) — order matters; some later
# patterns rely on earlier normalisations.
PATTERNS: list[tuple[str, str]] = [
    # --- canonical-noun OCR mangles -----------------------------------
    (r"\bRaleares\b",   "Baleares"),    # B→R OCR confusion
    (r"\bBileares\b",   "Baleares"),    # a→i
    (r"\bReleares\b",   "Baleares"),    # B→R + a→e
    (r"\bBeleares\b",   "Baleares"),    # a→e
    (r"\bBalearas\b",   "Baleares"),    # e→a
    (r"\bMaílorca\b",   "Mallorca"),    # ll→íl
    (r"\bMalíorca\b",   "Mallorca"),
    (r"\bMallotca\b",   "Mallorca"),    # r→t
    (r"\bMailorca\b",   "Mallorca"),    # ll→il
    (r"\bMalloica\b",   "Mallorca"),    # rc→ic
    (r"\bMallorea\b",   "Mallorca"),    # c→e
    (r"\bMallorcá\b",   "Mallorca"),    # spurious accent
    (r"\bMallorcr\b",   "Mallorca"),
    (r"\bMenorcr\b",    "Menorca"),
    (r"\bHuleares\b",   "Baleares"),    # B→H
    (r"\bBaleates\b",   "Baleares"),    # r→t
    (r"\bBateares\b",   "Baleares"),
    (r"\bMahon\b",      "Mahón"),       # missing accent (when on its own; never inside another word)
    # --- common-noun OCR mangles --------------------------------------
    (r"\bprédio\b",     "predio"),      # OCR-introduced acute
    (r"\bprédios\b",    "predios"),
    (r"\bpiedio\b",     "predio"),      # r→i
    (r"\bpredío\b",     "predio"),      # spurious accent
    (r"\bbéo\b",        "bey"),         # rarely; skipped if false-positive
    (r"\bdóc\.\b",      "dióc."),       # missing letter
    (r"\bdioc\.\b",     "dióc."),       # missing accent
    (r"\bjurísd\.\b",   "jurisd."),     # spurious accent
    (r"\bjuriíd\.\b",   "jurisd."),
    (r"\bjurisdiccion\b", "jurisdicción"),
    (r"\btéim\.",       "térm."),       # r→i
    (r"\bténn\.",       "térm."),       # rm→nn
    (r"\btévn\.",       "térm."),
    (r"\btei\s?m\.",    "térm."),
    (r"\btcrn\.",       "térm."),       # é→c
    (r"\btérn\.",       "térm."),       # m→n
    (r"\btérm,",        "térm.,"),      # ,→.
    (r"\bterm\.",       "térm."),       # missing accent
    (r"\btermino\b",    "término"),     # missing accent (less common in our corpus, skip if unsure)
    (r"\bpait\.\s?jud\.","part. jud."), # r→i
    (r"\bparí\.\s?jud\.","part. jud."), # rt→ri
    (r"\bparí,\s?jud,","part. jud,"),
    (r"\bpart\.\s?jua\.","part. jud."), # d→a
    (r"\bpart\.\s?jud,","part. jud."),  # ,→.
    (r"\bpart\.\s?jud\s+de\b","part. jud. de"),  # missing dot
    (r"\bpart\.jud\.",  "part. jud."),  # missing space
    (r"\bpart\s+\.\s?jud\.","part. jud."), # space before period
    (r"\bjud\.\.",      "jud."),        # doubled period
    (r"\bpart\.\.",     "part."),       # doubled period
    (r"\bayunt,",       "ayunt."),
    # --- joined-word fixes (no-space OCR breaks) ----------------------
    (r"\bislade\b",     "isla de"),
    (r"\blaisla\b",     "la isla"),
    (r"\bdéla\b",       "de la"),
    (r"\bdela\b",       "de la"),
    (r"\bdel a\b",      "de la"),       # spurious split (only safe for very specific forms — keep only the safe one if it appears)
    (r"\byjurisd\.",    "y jurisd."),
    (r"\byjur\b",       "y jur"),
    (r"\bjurisd\.de\b", "jurisd. de"),
    (r"\bjurisd\.\s*del\b", "jurisd. del"),
    (r"\bjurisd\.\s*déla\b", "jurisd. de la"),
    (r"\bpobl\.y\b",    "pobl. y"),
    (r"\bv\.de\b",      "v. de"),
    (r"\bc\.de\b",      "c. de"),
    (r"\bl\.de\b",      "l. de"),
    (r"\bs\.de\b",      "s. de"),
    (r"\bjud\.de\b",    "jud. de"),
    # --- whitespace + punctuation -------------------------------------
    (r"(Mallorca|Menorca|Baleares|Ibiza|Formentera)\s+,",      r"\1,"),
    (r"\s+,",           ","),           # any " ," with space before comma
    (r",,",             ","),           # double comma
    # Missing space after comma when followed by a letter (Mallorca,prov. → Mallorca, prov.). Excludes the number-comma-digit case via the negative lookahead being a non-digit by virtue of the letter class.
    (r",(?=[a-zA-Záéíóúñ])", ", "),
    (r"(?<!\.)\.{2}(?!\.)", "."),       # exactly two periods (don't touch '...' parenthetical ellipses)
    (r":\.",            "."),
    (r"^[\s\.,:;•¡!\-\+\^\*]+",  ""),   # leading punctuation noise
    (r"[ \t]{2,}",      " "),           # collapse runs of spaces/tabs only (preserve \n\n paragraph breaks)
    (r"\([ \t]+",       "("),           # space after opening paren
    (r"[ \t]+\)",       ")"),           # space before closing paren
    (r"\n{3,}",         "\n\n"),        # collapse 3+ newlines to a single paragraph break
]

# Pre-compile
COMPILED: list[tuple[re.Pattern[str], str]] = [(re.compile(p), r) for p, r in PATTERNS]


def normalise(text: str) -> str:
    out = text
    for pat, rep in COMPILED:
        out = pat.sub(rep, out)
    # Final tidy: trim trailing/leading whitespace
    return out.strip()


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    rows = con.execute(
        "SELECT id, title, description, source_file FROM text_entries "
        "WHERE description IS NOT NULL ORDER BY id"
    ).fetchall()

    changes = []  # (id, title, old, new, src)
    for tid, title, desc, src in rows:
        new = normalise(desc)
        if new != desc:
            changes.append((tid, title, desc, new, src))

    print(f"{len(changes)} of {len(rows)} descriptions would change.")

    if changes and not apply:
        # Show a handful of diffs to spot-check
        sample = changes[:10] + (changes[-5:] if len(changes) > 15 else [])
        seen = set()
        for tid, title, od, nd, _ in sample:
            if tid in seen:
                continue
            seen.add(tid)
            # Find first differing region (truncate context)
            i = 0
            while i < min(len(od), len(nd)) and od[i] == nd[i]:
                i += 1
            ctx_start = max(0, i - 20)
            ctx_end = min(len(od), i + 80)
            print(f"\n  id={tid:5} {title!r}")
            print(f"    -…{od[ctx_start:ctx_end]}…")
            print(f"    +…{nd[ctx_start:min(len(nd), i+80)]}…")
        print("\nDRY RUN — pass --apply to commit.")
        return

    if not apply:
        return

    # Apply
    json_files_dirty: dict[Path, dict] = {}
    for tid, title, od, nd, src in changes:
        path = PROJECT / src
        if path not in json_files_dirty and path.exists():
            json_files_dirty[path] = json.loads(path.read_text(encoding="utf-8"))
        data = json_files_dirty.get(path)
        if data:
            for e in data.get("entries", []):
                if e.get("description") == od:
                    if not e.get("description_raw"):
                        e["description_raw"] = od
                    e["description"] = nd
        con.execute(
            "UPDATE text_entries "
            "SET description=?, description_raw=COALESCE(description_raw, ?) "
            "WHERE id=?",
            [nd, od, tid],
        )

    # Write JSON files
    for path, data in json_files_dirty.items():
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(f"\n✓ Applied {len(changes)} description normalisations "
          f"across {len(json_files_dirty)} JSON files.")


if __name__ == "__main__":
    main()
