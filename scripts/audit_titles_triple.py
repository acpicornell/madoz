"""Triple-OCR title audit — methodologically clean version.

For every text_entries row we triangulate THREE independent OCR
readings of the same PDF leaf:

  ABBYY     = chocr_entries.title (raw OCR, untouched by curation).
              Linked via text_entries.chocr_entry_id when present.
              Falls back to the closest fuzzy match across all
              chocr_entries on the same (vol, leaf) page.
  Tesseract = closest opener line in
              data/tesseract/text/tomoVOL_p(leaf-1).txt
  Vision    = closest opener line in
              data/applevision/text/tomoVOL_p(leaf-1).txt

The DB title (text_entries.title) is shown alongside as REFERENCE
only — it has been post-processed by cleanup_unverified.py and is
NOT part of the OCR triangulation. We are asking: when the three
independent OCR engines disagree, who is right?

Verdicts (computed on compressed-lemma form with parens stripped):

  unanimous_3       — all three OCRs agree.
  2v1_abbyy_outlier — Tess+Vision agree, ABBYY differs.
                      → strongest signal that the curated title
                        (which followed ABBYY) may be wrong.
  2v1_tess_outlier  — ABBYY+Vision agree, Tess differs.
  2v1_vision_outlier — ABBYY+Tess agree, Vision differs.
  all_differ        — three different readings; manual review.
  abbyy_only / two_missing — fewer than two readings available.

Then a second axis: does the DB curated title match the OCR
consensus? If `unanimous_3` but DB ≠ consensus, the curatorial
edit may have over-corrected.

Read-only. Reports counts + per-verdict lists for inspection.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import duckdb
from rapidfuzz import fuzz, process

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
TESS_DIR = PROJECT / "data" / "tesseract" / "text"
VISN_DIR = PROJECT / "data" / "applevision" / "text"

# Opener pattern: an uppercase headword followed by Madoz separator.
OPENER_RE = re.compile(
    r"^\s*([A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ0-9.\s\(\)\-,/]{2,60}?)\s*[:.,;]",
    re.MULTILINE,
)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def compress(s: str) -> str:
    """Bare-lemma compressed form. Parenthetical suffixes ((Son),
    (predio), (cabo, …)) are stripped — OCR rarely captures them and
    they aren't part of the lemma proper."""
    if not s:
        return ""
    s = re.sub(r"\([^)]*\)", "", s)
    s = _strip_accents(s).upper()
    return re.sub(r"[^A-Z0-9]", "", s)


def find_closest_opener_in_text(text: str, ck_target: str,
                                threshold: int = 90):
    """Find the opener line in `text` whose compressed lemma best
    matches `ck_target`. Returns (lemma_raw, fuzz) or (None, 0)."""
    if not text or not ck_target:
        return (None, 0)
    best = (None, 0)
    for m in OPENER_RE.finditer(text):
        cand = m.group(1).strip()
        ck = compress(cand)
        if not ck:
            continue
        score = fuzz.ratio(ck_target, ck)
        if score > best[1]:
            best = (cand, score)
    if best[1] < threshold:
        return (None, 0)
    return best


def find_closest_chocr(con, vol: str, leaf: int, ck_target: str,
                       threshold: int = 80):
    """Find the chocr opener on the same (vol, leaf) page that best
    matches `ck_target`. Returns (chocr_id, title, fuzz) or
    (None, None, 0). The threshold is looser than the Tess/Vision
    matcher because we're scanning *opener candidates* (not full page
    text), which already have the headword extracted cleanly."""
    rows = con.execute(
        "SELECT id, title FROM chocr_entries "
        "WHERE vol=? AND leaf=?", [vol, leaf]
    ).fetchall()
    if not rows:
        return (None, None, 0)
    best = (None, None, 0)
    for cid, title in rows:
        ck = compress(title or "")
        if not ck:
            continue
        score = fuzz.ratio(ck_target, ck)
        if score > best[2]:
            best = (cid, title, score)
    if best[2] < threshold:
        return (None, None, 0)
    return best


def main():
    con = duckdb.connect(str(DB), read_only=True)
    rows = con.execute("""
        SELECT te.id, te.vol, te.leaf, te.title AS db_title,
               te.chocr_entry_id, ce.title AS abbyy_linked
        FROM text_entries te
        LEFT JOIN chocr_entries ce ON ce.id = te.chocr_entry_id
        WHERE te.vol IS NOT NULL AND te.leaf IS NOT NULL AND te.leaf > 0
        ORDER BY te.id
    """).fetchall()
    print(f"Auditing {len(rows)} text_entries (vol, leaf > 0)...\n")

    verdicts = []
    for tid, vol, leaf, db_title, ce_id, abbyy_linked in rows:
        ck_target = compress(db_title)
        # ABBYY raw reading
        if abbyy_linked:
            abbyy_raw = abbyy_linked
        else:
            _cid, abbyy_raw, _sc = find_closest_chocr(con, vol, leaf,
                                                     ck_target)
        # Tesseract reading
        tess_path = TESS_DIR / f"tomo{vol}_p{leaf - 1:04d}.txt"
        tess_text = (tess_path.read_text(encoding="utf-8", errors="replace")
                     if tess_path.exists() else "")
        tess_raw, tess_score = find_closest_opener_in_text(tess_text,
                                                           ck_target)
        # Vision reading
        visn_path = VISN_DIR / f"tomo{vol}_p{leaf - 1:04d}.txt"
        visn_text = (visn_path.read_text(encoding="utf-8", errors="replace")
                     if visn_path.exists() else "")
        visn_raw, visn_score = find_closest_opener_in_text(visn_text,
                                                           ck_target)

        ck_a = compress(abbyy_raw or "")
        ck_t = compress(tess_raw or "")
        ck_v = compress(visn_raw or "")
        present = sum(bool(x) for x in (ck_a, ck_t, ck_v))

        if present < 2:
            verdict = "fewer_than_2_sources"
        else:
            # All three present? compare 3-way. Else compare the 2.
            if present == 3:
                a_t = ck_a == ck_t
                a_v = ck_a == ck_v
                t_v = ck_t == ck_v
                if a_t and a_v:
                    verdict = "unanimous_3"
                elif t_v and not a_t:
                    verdict = "2v1_abbyy_outlier"
                elif a_v and not t_v:
                    verdict = "2v1_tess_outlier"
                elif a_t and not t_v:
                    verdict = "2v1_vision_outlier"
                else:
                    verdict = "all_differ"
            else:
                # Two of three present.
                pair = [(name, ck) for name, ck in
                        (("A", ck_a), ("T", ck_t), ("V", ck_v)) if ck]
                if pair[0][1] == pair[1][1]:
                    verdict = f"agree_2_of_2_{pair[0][0]}{pair[1][0]}"
                else:
                    verdict = f"disagree_2_of_2_{pair[0][0]}{pair[1][0]}"

        # DB vs OCR-consensus axis: only meaningful when consensus exists.
        # consensus = unanimous_3 reading, or the 2-vote side in 2v1, or
        # the agree side in agree_2_of_2.
        consensus_ck = None
        if verdict == "unanimous_3":
            consensus_ck = ck_a
        elif verdict == "2v1_abbyy_outlier":
            consensus_ck = ck_t  # = ck_v
        elif verdict == "2v1_tess_outlier":
            consensus_ck = ck_a  # = ck_v
        elif verdict == "2v1_vision_outlier":
            consensus_ck = ck_a  # = ck_t
        elif verdict.startswith("agree_2_of_2"):
            pair = [(name, ck) for name, ck in
                    (("A", ck_a), ("T", ck_t), ("V", ck_v)) if ck]
            consensus_ck = pair[0][1]

        db_matches_consensus = (consensus_ck is not None
                                and ck_target == consensus_ck)

        verdicts.append({
            "id": tid, "vol": vol, "leaf": leaf,
            "db": db_title, "abbyy": abbyy_raw,
            "tess": tess_raw, "vision": visn_raw,
            "verdict": verdict,
            "consensus_ck": consensus_ck,
            "db_matches": db_matches_consensus,
        })
    con.close()

    counter = Counter(v["verdict"] for v in verdicts)
    print("=== Verdicts (3-way OCR triangulation) ===")
    for k, n in counter.most_common():
        print(f"  {k:30s} {n:5d}  ({n/len(verdicts)*100:.1f}%)")
    print()

    # Second axis: where does DB diverge from OCR consensus?
    divergent = [v for v in verdicts
                 if v["consensus_ck"] is not None and not v["db_matches"]]
    print(f"=== DB title diverges from OCR consensus: "
          f"{len(divergent)} rows ===\n")
    # Sub-group by verdict
    by_verdict = defaultdict(list)
    for v in divergent:
        by_verdict[v["verdict"]].append(v)

    for verdict in ["unanimous_3", "2v1_abbyy_outlier", "2v1_tess_outlier",
                    "2v1_vision_outlier",
                    "agree_2_of_2_TV", "agree_2_of_2_AT", "agree_2_of_2_AV"]:
        bucket = by_verdict.get(verdict, [])
        if not bucket:
            continue
        print(f"--- {verdict}  ({len(bucket)} rows) ---")
        for v in bucket[:40]:
            print(f"  id={v['id']:5d}  v{v['vol']} l{v['leaf']:4d}")
            print(f"    DB:     {v['db']!r}")
            print(f"    ABBYY:  {v['abbyy']!r}")
            print(f"    Tess:   {v['tess']!r}")
            print(f"    Vision: {v['vision']!r}")
            print()
        if len(bucket) > 40:
            print(f"    ... and {len(bucket) - 40} more\n")

    # all_differ — manual review
    diffs = [v for v in verdicts if v["verdict"] == "all_differ"]
    if diffs:
        print(f"=== all_differ ({len(diffs)} rows; manual review) ===\n")
        for v in diffs[:20]:
            print(f"  id={v['id']:5d}  v{v['vol']} l{v['leaf']:4d}")
            print(f"    DB:     {v['db']!r}")
            print(f"    ABBYY:  {v['abbyy']!r}")
            print(f"    Tess:   {v['tess']!r}")
            print(f"    Vision: {v['vision']!r}")
            print()


if __name__ == "__main__":
    main()
