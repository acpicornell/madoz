"""Audit text_entries.title for residual OCR-mangled forms.

Two passes:

1. Pattern check — regex tripwires for known OCR damage signals
   (digits inside lemma, junk chars like \\ ¡ ¿, double-letter suffix
   like 'II'/'MM' that's usually a misread 'H', short-split lemmas
   like 'ADA YA' that may be the OCR splitting a single word).
   False-positive prone (legitimate Mallorquí compounds 'CAS X' /
   'CA X' / 'POU X' / 'TOR DE X' / 'SON X' / 'PI DE X' etc. trip
   `split_short_lemma`) but high recall on real damage.

2. Tesseract cross-check — for every flagged title, look up the
   corresponding tesseract page text (if available under
   data/tesseract/text/) and see what Tesseract reads at the same
   leaf opener. A mismatch is the strongest signal that the DB's
   title is the OCR-mangled one.

Read-only. Reports candidates; never writes.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
TESS = PROJECT / "data" / "tesseract" / "text"


PATTERNS = [
    (re.compile(r"^[^()]*\d"),
     "digit_in_lemma",
     "ABBYY substituted a digit for a letter (1↔I, 0↔O, 5↔S, 4↔A, 8↔B)"),
    (re.compile(r"[\\|{}~`^]"),
     "junk_chars",
     "non-Madoz junk character"),
    (re.compile(r"[¡¿]|[‘’“”]"),
     "weird_quotes",
     "OCR-mangled punctuation"),
    (re.compile(r"^[^(]*[A-Z][a-z][A-Z]"),
     "case_jitter",
     "lowercase between caps suggests OCR misread"),
    (re.compile(r"(II|MM|NN|OO)$"),
     "double_letter_suffix",
     "II/MM at end of lemma usually = H/N OCR mangle"),
    (re.compile(r"^[A-ZÁÉÍÓÚÑ]{1,3} [A-ZÁÉÍÓÚÑ]{2,6}(?:\s|$|\()"),
     "split_short_lemma",
     "lemma like 'ADA YA' may be a split-word OCR error (but many "
     "Mallorquí compounds 'CAS X' / 'POU X' / 'TOR DE X' are real)"),
]


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def compressed(s: str) -> str:
    s = _strip_accents(s).upper()
    return re.sub(r"[^A-Z]", "", s)


def find_tesseract_reading(vol: str, leaf: int, current_title: str) -> str | None:
    """Find the closest Tesseract opener on the same leaf (PDF page
    index = leaf - 1, typically). Return the opener line if a clear
    match exists, else None."""
    if not TESS.exists():
        return None
    # Try (leaf - 1), then ±1
    for off in (0, -1, 1, -2, 2):
        pdf_p = (leaf - 1) + off
        if pdf_p < 0:
            continue
        path = TESS / f"tomo{vol}_p{pdf_p:04d}.txt"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        ck = compressed(current_title)
        # Find every all-caps opener
        best = None
        best_score = 0
        from rapidfuzz import fuzz
        for m in re.finditer(
            r"^\s*([A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ.\s\(\)\-,/]{2,60}?)\s*[:.,;]",
            text, re.MULTILINE,
        ):
            cand = m.group(1).strip()
            score = fuzz.ratio(ck, compressed(cand))
            if score > best_score:
                best_score = score
                best = cand
        if best and best_score >= 50:
            return f"{best} (fuzz={best_score:.0f}, pdf p{pdf_p})"
    return None


def main():
    con = duckdb.connect(str(DB), read_only=True)
    rows = con.execute(
        "SELECT id, vol, leaf, title FROM text_entries ORDER BY id"
    ).fetchall()
    con.close()

    flagged = []
    for tid, vol, leaf, title in rows:
        if not title:
            continue
        bare = re.sub(r"\([^)]*\)", "", title).strip()
        for pat, name, _why in PATTERNS:
            if pat.search(bare):
                flagged.append((tid, vol, leaf, title, name))
                break

    print(f"Audited {len(rows)} text_entries titles")
    print(f"Flagged by pattern: {len(flagged)}\n")

    from collections import Counter
    cnt = Counter(f[4] for f in flagged)
    for name, n in cnt.most_common():
        print(f"  {n:3d}  {name}")

    if not TESS.exists():
        print("\n(Tesseract output not present; pattern audit only.)")
        return

    print("\n=== Tesseract cross-check on flagged titles ===\n")
    mismatches = []
    for tid, vol, leaf, title, rule in flagged:
        reading = find_tesseract_reading(vol, leaf, title)
        if reading is None:
            continue
        # Strip parens for comparison
        db_bare = re.sub(r"\([^)]*\)", "", title).strip()
        tess_bare = re.sub(r"\([^)]*\)", "", reading.split(" (fuzz")[0]).strip()
        if compressed(db_bare) != compressed(tess_bare):
            mismatches.append((tid, vol, leaf, title, reading, rule))

    print(f"DB title ≠ Tesseract reading: {len(mismatches)}\n")
    for tid, vol, leaf, title, reading, rule in mismatches:
        print(f"  [{rule}]")
        print(f"    id={tid} v{vol} l{leaf}")
        print(f"    DB:        {title!r}")
        print(f"    Tesseract: {reading}")
        print()


if __name__ == "__main__":
    main()
