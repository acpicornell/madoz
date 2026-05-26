"""Cross-link text_entries to chocr_entries by fuzzy title match.

For each row in text_entries, fill chocr_entry_id: best title match on
the same (vol, leaf) in chocr_entries.

Normalization handles common OCR mangles:
  - Ñ → N, accents stripped
  - B ↔ V → B (e.g. VANALHUFAR ≈ BANALBUFAR)
  - OCR digit confusions inside words: 1→i, 0→o, 5→s, 8→b
    (FELAN1TX → FELANITX)
  - Parens stripped; punctuation, hyphens collapsed; lowercase

Threshold: 0.60 (candidate set is small — limited to the same leaf).

Re-runnable; only updates NULL columns.
"""
from __future__ import annotations

import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

# OCR digit→letter confusions inside words (not at start)
_DIGIT_FIX = str.maketrans({"1": "i", "0": "o", "5": "s", "8": "b"})


def normalize(title: str) -> str:
    if not title:
        return ""
    s = title.lower().strip()
    # strip accents
    s = "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )
    # strip parenthesized / bracketed suffixes/prefixes
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    # OCR digit fixes only when adjacent to letters
    s = re.sub(
        r"(?<=[a-z])([105 8])(?=[a-z])",
        lambda m: m.group(1).translate(_DIGIT_FIX),
        s,
    )
    s = re.sub(
        r"(?<=[a-z])([105 8])$",
        lambda m: m.group(1).translate(_DIGIT_FIX),
        s,
    )
    # B↔V canonicalize
    s = s.replace("v", "b")
    # Ñ→N (already handled by accent strip but defensive)
    s = s.replace("ñ", "n")
    # remove hyphens/commas/dots/etc
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def link_chocr(con: duckdb.DuckDBPyConnection, threshold: float = 0.85) -> tuple[int, list]:
    """Match text_entries to chocr_entries on same (vol, leaf).

    Strategy:
      1. Exact normalized match → assign.
      2. text-title startswith chocr-title (or vice versa) → assign.
         Catches mega sub-entries: "ALAYOR (peñas y atalaya)" → "ALAYOR".
      3. Fuzzy ≥ threshold → assign. Catches chocr OCR mangles
         ("ALC ARIAS" → "ALCARIAS", "LLÜÍVIE'NA" → "LLUMENA").
    Many-to-one is allowed: one chocr index entry may legitimately back
    several text entries (mega articles split into c. + part. jud.).
    """
    rows = con.execute(
        """SELECT id, vol, leaf, title FROM text_entries
           WHERE chocr_entry_id IS NULL"""
    ).fetchall()

    updates: list[tuple[int, int]] = []
    misses: list[tuple] = []
    for tid, vol, leaf, title in rows:
        ntitle = normalize(title)
        cands = con.execute(
            "SELECT id, title FROM chocr_entries WHERE vol=? AND leaf=?",
            [vol, leaf],
        ).fetchall()
        if not cands:
            misses.append((tid, vol, leaf, title, "no chocr on this leaf"))
            continue

        chosen = None
        # 1. exact
        for cid, ct in cands:
            if normalize(ct) == ntitle:
                chosen = (cid, ct, 1.0)
                break
        # 2. startswith
        if not chosen:
            for cid, ct in cands:
                nct = normalize(ct)
                if not nct or not ntitle:
                    continue
                if ntitle.startswith(nct + " ") or nct.startswith(ntitle + " "):
                    chosen = (cid, ct, 0.99)
                    break
        # 3. fuzzy
        if not chosen:
            scored = sorted(
                ((similarity(ntitle, normalize(ct)), cid, ct) for cid, ct in cands),
                reverse=True,
            )
            if scored and scored[0][0] >= threshold:
                chosen = (scored[0][1], scored[0][2], scored[0][0])

        if chosen:
            updates.append((chosen[0], tid))
        else:
            best = max(
                ((similarity(ntitle, normalize(ct)), ct) for cid, ct in cands),
                default=(0, None),
            )
            misses.append((tid, vol, leaf, title, f"best={best[1]!r}@{best[0]:.2f}"))

    if updates:
        con.executemany(
            "UPDATE text_entries SET chocr_entry_id=? WHERE id=?", updates
        )
    return len(updates), misses


def main() -> None:
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB))
    print("Linking text_entries → chocr_entries (same vol/leaf)...")
    nc, miss_c = link_chocr(con)
    print(f"  matched {nc} rows; {len(miss_c)} unmatched")
    for m in miss_c[:20]:
        print(f"    miss: tom{m[1]} leaf {m[2]}  {m[3]!r}  ({m[4]})")
    if len(miss_c) > 20:
        print(f"    ... and {len(miss_c)-20} more")

    print()
    # Summary
    n = con.execute("SELECT COUNT(*) FROM text_entries").fetchone()[0]
    nc_total = con.execute(
        "SELECT COUNT(*) FROM text_entries WHERE chocr_entry_id IS NOT NULL"
    ).fetchone()[0]
    print(f"Final: {n} text_entries  |  chocr-linked: {nc_total}")


if __name__ == "__main__":
    main()


