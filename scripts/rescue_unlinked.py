"""Rescue text_entries ↔ madoz_entries links that the main linker missed.

Three phases, each gated by --apply:

  1. Fill NULL madoz_entry_id where text title + place_type
     unambiguously matches a curated entry. Handles the multi-sense case
     the main linker can't disambiguate (MALLORCA provincia vs audiencia
     vs diócesis — title bare-name collides; place_type breaks the tie).

  2. Relink wrong-sense FKs: text_entry currently linked to a madoz_entry
     whose place_type clashes with the text_entry's place_type, AND a
     better candidate (same bare-lemma, matching place_type) exists.
     Only relinks when target is unambiguous.

  3. Promote chocr-only Balearic articles into text_entries: the chocr
     paragraph segmenter found them but Phase 1 of the main pipeline
     dropped them as non-Balearic. They survive in chocr_entries with
     madoz_entry_id=NULL, and a curated-mirror entry confirms they ARE
     Balearic. Insert a new text_entries row with the chocr OCR snippet
     as description (confidence='unverified', model='chocr-snippet').

Run dry first, then with --apply.
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
CHOCR_DIR = PROJECT / "data" / "text" / "_chocr"

# Castilian-form ↔ project-canonical form. Madoz uses Castilian spelling
# (DEVA, BAYOLI); our text_entries sometimes use the Catalan form (DEYA).
LEMMA_ALIAS = {
    "DEVA": "DEYA",
    "DEYA": "DEVA",
}

# Tokens that confirm a text body is talking about the Balearic Islands.
# Used in Phase 3 to reject curated false positives (e.g. ALCORNOCAL, an
# arroyo in Badajoz that the curated mirror mis-categorised as Balearic).
BALEARIC_TOKENS = re.compile(
    r"\b(?:Mallorca|Menorca|Iviza|Ibiza|Baleares|Cabrera|Formentera|"
    r"Palma\s+de\s+Mallorca|Mah[óo]n|Ciudadela|Eivissa)\b",
    re.IGNORECASE,
)

# Map of opening abbreviations Madoz uses to a normalized place_type.
# Used to recover place_type from chocr context when chocr_entries.place_type
# is NULL, so Phase 3 can disambiguate between same-lemma curated senses
# (SALINAS cabo vs SALINAS aldea).
PT_OPENERS = [
    (re.compile(r"^\s*v\.\s+con", re.I),       "villa"),
    (re.compile(r"^\s*v\.\s+cab", re.I),       "villa"),
    (re.compile(r"^\s*villa\b", re.I),         "villa"),
    (re.compile(r"^\s*c\.\s+con", re.I),       "ciudad"),
    (re.compile(r"^\s*ciudad\b", re.I),        "ciudad"),
    (re.compile(r"^\s*l\.\s+(?:con|de|en)", re.I), "lugar"),
    (re.compile(r"^\s*lugar\b", re.I),         "lugar"),
    (re.compile(r"^\s*ald\.\b", re.I),         "aldea"),
    (re.compile(r"^\s*aldea\b", re.I),         "aldea"),
    (re.compile(r"^\s*cas(?:erío|\.)\b", re.I),"caserío"),
    (re.compile(r"^\s*predio\s+con", re.I),    "predio"),
    (re.compile(r"^\s*predio\b", re.I),        "predio"),
    (re.compile(r"^\s*alqu[eé]r[ií]a\b", re.I),"alquería"),
    (re.compile(r"^\s*finca\b", re.I),         "finca"),
    (re.compile(r"^\s*cabo\b", re.I),          "cabo"),
    (re.compile(r"^\s*cala\b", re.I),          "cala"),
    (re.compile(r"^\s*bah[ií]a\b", re.I),      "bahía"),
    (re.compile(r"^\s*isla\b", re.I),          "isla"),
    (re.compile(r"^\s*isleta\b", re.I),        "isleta"),
    (re.compile(r"^\s*islote\b", re.I),        "islote"),
    (re.compile(r"^\s*r[ií]o\b|^\s*r\.\b", re.I), "río"),
    (re.compile(r"^\s*arroyo\b", re.I),        "arroyo"),
    (re.compile(r"^\s*sierra\b", re.I),        "sierra"),
    (re.compile(r"^\s*monte\b", re.I),         "monte"),
    (re.compile(r"^\s*partido\s+jud", re.I),   "partido judicial"),
    (re.compile(r"^\s*part\.\s*jud", re.I),    "partido judicial"),
    (re.compile(r"^\s*di[óo]c", re.I),         "diócesis"),
    (re.compile(r"^\s*audiencia\b", re.I),     "audiencia"),
    (re.compile(r"^\s*provincia\b", re.I),     "provincia"),
    (re.compile(r"^\s*felig", re.I),           "feligresía"),
    (re.compile(r"^\s*parroquia\b", re.I),     "parroquia"),
    (re.compile(r"^\s*balsa\b", re.I),         "balsa"),
    (re.compile(r"^\s*torre\b", re.I),         "torre"),
    (re.compile(r"^\s*puerto\b|^\s*puerto?\b", re.I), "puerto"),
]


def infer_place_type(context_or_para: str | None) -> str | None:
    """Read the place_type from the opening of an article body."""
    if not context_or_para:
        return None
    # Strip leading title + separator (`LEMMA : body...`)
    s = re.sub(r"^[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ.\s\(\)\-,]{0,80}?[:.,]\s*", "", context_or_para)
    for pat, pt in PT_OPENERS:
        if pat.match(s):
            return pt
    return None


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def core_lemma(title: str) -> str:
    """Normalize a title to its bare lemma for matching.

    Strips parentheticals/brackets, trailing qualifiers ('vulgo X', '— X'),
    accents, B↔V, Ñ→N, common Saint prefixes, articles, and punctuation.
    """
    if not title:
        return ""
    s = title.upper().strip()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\s+[—\-]\s+.*$", " ", s)
    # Drop trailing 'VULGO X', 'ANT(IGUAMENTE) X' name variants
    s = re.sub(r"\bVULGO\b.*$", " ", s)
    s = re.sub(r"\bANT(?:IGUO|IGUAMENTE|IG\.|\.)\b.*$", " ", s)
    s = _strip_accents(s)
    s = re.sub(r"\b(SANTA|SANTO|SAN|SO|SON|LA|LAS|LOS|EL)\b", " ", s)
    s = s.replace("V", "B").replace("Ñ", "N")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def pt_aligned(text_pt: str | None, madoz_pt: str | None) -> int:
    """Score how well two place_type strings align (0..3).

    3 = exact, 2 = one is prefix of other, 1 = same first token,
    0 = mismatch. None on either side returns 0 (no info, can't trust).
    """
    if not text_pt or not madoz_pt:
        return 0
    tp = text_pt.lower().strip()
    mp = madoz_pt.lower().strip()
    if tp == mp:
        return 3
    if tp.startswith(mp) or mp.startswith(tp):
        return 2
    if tp.split()[0] == mp.split()[0]:
        return 1
    return 0


def phase1_fill_nulls(con: duckdb.DuckDBPyConnection, *, apply: bool) -> list[dict]:
    """Fill NULL madoz_entry_id by core-lemma + place_type matching.

    Conservative: only links when there's a place-type-aligned candidate
    that is itself unambiguous (no other candidate with same lemma + pt
    score). Skips ambiguous cases.
    """
    text_rows = con.execute("""
        SELECT id, title, place_type FROM text_entries
        WHERE madoz_entry_id IS NULL
    """).fetchall()
    madoz_rows = con.execute("""
        SELECT id, title, place_type FROM madoz_entries
    """).fetchall()

    # Index madoz_entries by core_lemma (and by aliased lemma)
    by_lemma: dict[str, list[tuple[int, str, str | None]]] = {}
    for mid, mt, mpt in madoz_rows:
        cl = core_lemma(mt)
        if cl:
            by_lemma.setdefault(cl, []).append((mid, mt, mpt))

    proposed = []
    for tid, ttitle, tpt in text_rows:
        cl = core_lemma(ttitle)
        candidates = list(by_lemma.get(cl, []))
        # Try alias
        alias = LEMMA_ALIAS.get(cl)
        if alias:
            candidates.extend(by_lemma.get(alias, []))
        if not candidates:
            continue
        # Score by place_type alignment
        scored = sorted(
            ((pt_aligned(tpt, mpt), mid, mt, mpt) for mid, mt, mpt in candidates),
            reverse=True,
        )
        top_score = scored[0][0]
        top_candidates = [s for s in scored if s[0] == top_score]
        if len(top_candidates) == 1 and top_score >= 1:
            _, mid, mt, mpt = top_candidates[0]
            proposed.append({
                "phase": 1, "tid": tid, "ttitle": ttitle, "tpt": tpt,
                "mid": mid, "mt": mt, "mpt": mpt, "score": top_score,
            })
        elif len(top_candidates) == 1 and top_score == 0 and len(candidates) == 1:
            # Only candidate, but no place_type info on either side.
            # Accept anyway — this is the common "single curated entry,
            # no place_type metadata" case (most curated stubs).
            _, mid, mt, mpt = top_candidates[0]
            proposed.append({
                "phase": 1, "tid": tid, "ttitle": ttitle, "tpt": tpt,
                "mid": mid, "mt": mt, "mpt": mpt, "score": 0,
            })

    if apply:
        con.executemany(
            "UPDATE text_entries SET madoz_entry_id=? WHERE id=?",
            [(p["mid"], p["tid"]) for p in proposed],
        )
    return proposed


def phase2_relink_wrong_sense(
    con: duckdb.DuckDBPyConnection, *, apply: bool
) -> list[dict]:
    """For text_entries already linked, swap to a better-aligned candidate
    when (a) current link has place_type mismatch (score 0), (b) a strictly
    better candidate exists for the same bare lemma, (c) target is unambiguous.

    Never sets a link to NULL.
    """
    text_rows = con.execute("""
        SELECT t.id, t.title, t.place_type, t.madoz_entry_id,
               m.title, m.place_type
        FROM text_entries t
        JOIN madoz_entries m ON t.madoz_entry_id = m.id
    """).fetchall()
    madoz_rows = con.execute("""
        SELECT id, title, place_type FROM madoz_entries
    """).fetchall()

    by_lemma: dict[str, list[tuple[int, str, str | None]]] = {}
    for mid, mt, mpt in madoz_rows:
        cl = core_lemma(mt)
        if cl:
            by_lemma.setdefault(cl, []).append((mid, mt, mpt))

    proposed = []
    for tid, ttitle, tpt, cur_mid, cur_mt, cur_mpt in text_rows:
        cur_score = pt_aligned(tpt, cur_mpt)
        if cur_score >= 2:
            continue  # current link is already good
        # Look for a strictly better candidate
        cl = core_lemma(ttitle)
        candidates = list(by_lemma.get(cl, []))
        alias = LEMMA_ALIAS.get(cl)
        if alias:
            candidates.extend(by_lemma.get(alias, []))
        scored = sorted(
            ((pt_aligned(tpt, mpt), mid, mt, mpt) for mid, mt, mpt in candidates),
            reverse=True,
        )
        if not scored:
            continue
        top_score = scored[0][0]
        if top_score <= cur_score:
            continue
        top_candidates = [s for s in scored if s[0] == top_score]
        if len(top_candidates) != 1:
            continue  # ambiguous, skip
        _, mid, mt, mpt = top_candidates[0]
        if mid == cur_mid:
            continue
        proposed.append({
            "phase": 2, "tid": tid, "ttitle": ttitle, "tpt": tpt,
            "cur_mid": cur_mid, "cur_mpt": cur_mpt,
            "new_mid": mid, "new_mt": mt, "new_mpt": mpt,
            "cur_score": cur_score, "new_score": top_score,
        })

    if apply:
        con.executemany(
            "UPDATE text_entries SET madoz_entry_id=? WHERE id=?",
            [(p["new_mid"], p["tid"]) for p in proposed],
        )
    return proposed


def _load_chocr_window(vol: str, leaf: int) -> str | None:
    p = CHOCR_DIR / f"page_{vol}_{leaf}.txt"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def _extract_paragraph(chocr_text: str, lemma_upper: str) -> str | None:
    """Find the Balearic paragraph in the chocr window that opens with
    the given lemma.

    Madoz repeats the same lemma across regions ('SANTA EUGENIA' in
    Girona, again in Mallorca) on a single leaf, alphabetised by
    sub-province. We collect every paragraph starting with the lemma,
    prefer those containing a Balearic token, and return the longest.

    Strips leaf-boundary markers and 3-char running headers ("824 PET")
    from the window text first, so a paragraph that crosses a leaf
    boundary in Madoz appears as one continuous paragraph here.
    """
    # Drop staging-script markers, leaf-boundary markers, and OCR running
    # headers ("NNN XXX" at line start — a printed page number followed
    # by 3-letter section header).
    cleaned = re.sub(r"^=== [^=\n]+ ===\s*$", "", chocr_text, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*#.*$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*\d{1,4}\s+[A-Z]{2,4}\s*$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    paras = re.split(r"\n\s*\n", cleaned)
    pat = re.compile(
        rf"^\s*{re.escape(lemma_upper)}\b(?:\s*\([^)]*\))?\s*[:.,]",
        re.MULTILINE,
    )
    matches: list[str] = []
    for para in paras:
        m = pat.search(para)
        if m:
            matches.append(para[m.start():].strip())
    if not matches:
        return None
    bal = [p for p in matches if BALEARIC_TOKENS.search(p)]
    pool = bal if bal else matches
    return max(pool, key=len)


def phase3_promote_chocr(
    con: duckdb.DuckDBPyConnection, *, apply: bool
) -> list[dict]:
    """For each madoz_entry without a text_entry pointing to it, look for
    a matching chocr_entry and promote it into text_entries.

    Madoz-centric (one madoz → at most one new text_entry) so chocr's
    overlapping-window duplicates (LLOBERA listed on leaves 507 *and*
    509) don't multiply rows.
    """
    # Substantive curated Balearic entries with no text_entry yet
    unlinked_madoz = con.execute("""
        SELECT id, title, place_type, island, municipality,
               content_length, content_text
        FROM madoz_entries m
        WHERE NOT EXISTS (
            SELECT 1 FROM text_entries t WHERE t.madoz_entry_id = m.id
        )
          AND content_length >= 200
    """).fetchall()

    # Index chocr by core_lemma → list of candidate rows.
    chocr_rows = con.execute("""
        SELECT c.id, c.vol, c.leaf, c.page_printed, c.title, c.context
        FROM chocr_entries c
        WHERE c.source = 'regex'
          AND c.madoz_entry_id IS NULL
    """).fetchall()
    chocr_by_lemma: dict[str, list[tuple]] = {}
    for c in chocr_rows:
        cl = core_lemma(c[4])
        if cl:
            chocr_by_lemma.setdefault(cl, []).append(c)

    # Existing (vol, leaf, lemma) to avoid inserting a duplicate row for
    # an article already present (under a slightly different title).
    existing = con.execute("SELECT vol, leaf, title FROM text_entries").fetchall()
    existing_idx: dict[tuple, set[str]] = {}
    for vol, leaf, t in existing:
        existing_idx.setdefault((vol, leaf), set()).add(core_lemma(t))

    next_id = (con.execute("SELECT max(id) FROM text_entries").fetchone()[0] or 0) + 1

    proposed = []
    for mid, mt, mpt, misl, mmuni, mlen, mtext in unlinked_madoz:
        cl = core_lemma(mt)
        if not cl or len(cl) < 4:
            continue
        candidates = list(chocr_by_lemma.get(cl, []))
        candidates += [
            r for a in [LEMMA_ALIAS.get(cl)] if a for r in chocr_by_lemma.get(a, [])
        ]
        if not candidates:
            continue

        # Pick the chocr candidate whose inferred place_type aligns with
        # the curated madoz_entry's place_type. If many align, take the
        # first (chocr's overlapping windows duplicate the same article
        # — any of them yields the same paragraph).
        scored = []
        for cid, vol, leaf, pp, ctitle, ctx in candidates:
            chocr_pt = infer_place_type(ctx)
            score = pt_aligned(chocr_pt, mpt) if chocr_pt and mpt else 0
            scored.append((score, cid, vol, leaf, pp, ctitle, ctx))
        scored.sort(reverse=True)

        # Try candidates in score order
        chosen = None
        for score, cid, vol, leaf, pp, ctitle, ctx in scored:
            # Skip if a text_entry on the same (vol, leaf) already has this lemma
            if cl in existing_idx.get((vol, leaf), set()):
                continue
            window = _load_chocr_window(vol, leaf)
            if window is None:
                continue
            para = _extract_paragraph(
                window, ctitle.split("(")[0].strip()
            )
            if para is None or len(para) < 120:
                continue
            if not BALEARIC_TOKENS.search(para):
                continue
            chosen = (cid, vol, leaf, pp, ctitle, para, score)
            break

        if not chosen:
            continue
        cid, vol, leaf, pp, ctitle, para, score = chosen
        proposed.append({
            "phase": 3, "new_tid": next_id, "vol": vol, "leaf": leaf,
            "page_printed": pp, "title": ctitle, "place_type": mpt,
            "island": misl, "municipality": mmuni,
            "description": para[:8000],
            "chocr_entry_id": cid, "madoz_entry_id": mid,
            "confidence": "unverified",
            "note": "Promoted by rescue_unlinked.py from chocr+curated reconciliation; raw OCR snippet, not Claude-extracted",
            "source_file": f"data/text/_chocr/page_{vol}_{leaf}.txt",
            "_pt_score": score,
        })
        existing_idx.setdefault((vol, leaf), set()).add(cl)
        next_id += 1

    if apply and proposed:
        for p in proposed:
            con.execute(
                """INSERT INTO text_entries
                   (id, vol, leaf, page_printed, title, place_type, island,
                    municipality, description, confidence, model, source_file,
                    note, chocr_entry_id, madoz_entry_id, extracted_at,
                    description_raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), ?)""",
                [
                    p["new_tid"], p["vol"], p["leaf"], p["page_printed"],
                    p["title"], p["place_type"], p["island"], p["municipality"],
                    p["description"], p["confidence"], "chocr-snippet",
                    p["source_file"], p["note"], p["chocr_entry_id"],
                    p["madoz_entry_id"], p["description"],
                ],
            )
    return proposed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Commit changes. Default is dry-run.")
    ap.add_argument("--phase", type=int, choices=[1, 2, 3],
                    help="Run only one phase.")
    args = ap.parse_args()

    if not DB.exists():
        sys.exit(f"DB not found: {DB}")

    con = duckdb.connect(str(DB), read_only=not args.apply)

    before_n = con.execute("SELECT count(*) FROM text_entries").fetchone()[0]
    before_linked = con.execute(
        "SELECT count(*) FROM text_entries WHERE madoz_entry_id IS NOT NULL"
    ).fetchone()[0]

    if args.phase in (None, 1):
        ph1 = phase1_fill_nulls(con, apply=args.apply)
        print(f"\n=== Phase 1: fill NULL madoz_entry_id ({len(ph1)} proposals) ===")
        for p in ph1[:40]:
            print(f"  text {p['tid']} \"{p['ttitle'][:35]:35s}\" pt={p['tpt'] or '—':18s}"
                  f" → madoz {p['mid']} \"{p['mt'][:25]:25s}\" pt={p['mpt'] or '—':18s} (pt-score {p['score']})")
        if len(ph1) > 40:
            print(f"  ... and {len(ph1) - 40} more")

    if args.phase in (None, 2):
        ph2 = phase2_relink_wrong_sense(con, apply=args.apply)
        print(f"\n=== Phase 2: relink wrong-sense FKs ({len(ph2)} proposals) ===")
        for p in ph2[:40]:
            print(f"  text {p['tid']} \"{p['ttitle'][:35]:35s}\" pt={p['tpt'] or '—':18s}"
                  f" : madoz {p['cur_mid']}→{p['new_mid']} (pt {p['cur_mpt'] or '—'} → {p['new_mpt'] or '—'},"
                  f" score {p['cur_score']}→{p['new_score']})")
        if len(ph2) > 40:
            print(f"  ... and {len(ph2) - 40} more")

    if args.phase in (None, 3):
        ph3 = phase3_promote_chocr(con, apply=args.apply)
        print(f"\n=== Phase 3: promote chocr-only Balearic articles ({len(ph3)} proposals) ===")
        for p in ph3[:40]:
            print(f"  new text {p['new_tid']}: \"{p['title'][:35]:35s}\""
                  f" vol={p['vol']} leaf={p['leaf']} → madoz {p['madoz_entry_id']}"
                  f" desc-len={len(p['description'])}")
        if len(ph3) > 40:
            print(f"  ... and {len(ph3) - 40} more")

    after_n = con.execute("SELECT count(*) FROM text_entries").fetchone()[0]
    after_linked = con.execute(
        "SELECT count(*) FROM text_entries WHERE madoz_entry_id IS NOT NULL"
    ).fetchone()[0]
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"\n=== Summary [{mode}] ===")
    print(f"  text_entries:    {before_n} → {after_n}")
    print(f"  with madoz link: {before_linked} → {after_linked}")
    print(f"  net coverage:    {after_n - before_n:+d} entries,"
          f" {after_linked - before_linked:+d} links")


if __name__ == "__main__":
    main()
