"""Verify the manual title corrections in cleanup_unverified.py against
a Tesseract re-OCR of the relevant PDF page.

For each entry in cleanup_unverified.TITLE_FIXES:
  1. Locate the PDF page (look up vol+leaf, find the page containing the
     OCR-mangled or canonical lemma).
  2. Render the page at 400 dpi via PyMuPDF.
  3. Run Tesseract with `spa` (modern Spanish) trained data.
  4. Search the Tesseract output for the canonical form, the ABBYY
     mangled form, and any plausible variants.
  5. Verdict: CONFIRM (canonical found), CONTRADICT (different reading
     found), UNCLEAR (neither found cleanly).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import duckdb
import fitz

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
TMP = Path("/tmp/madoz_tess")
TMP.mkdir(parents=True, exist_ok=True)

# Pull the manual title fixes from cleanup_unverified.py
sys.path.insert(0, str(PROJECT / "scripts"))
from cleanup_unverified import TITLE_FIXES


def find_page_with_lemma(doc, lemma: str, leaf_hint: int, window: int = 6):
    """Search around the chocr-claimed leaf for any PDF page whose text
    layer contains the lemma. Returns (pdf_page_index, location).

    chocr leaves don't map 1:1 to PDF pages (offset varies). Probe ±N.
    """
    lemma_bare = re.sub(r"\([^)]*\)", "", lemma).strip()
    candidates = [lemma_bare]
    # Also try canonical compress (uppercase letters only) for matching
    canon = re.sub(r"[^A-Z]", "", lemma_bare.upper())
    for offset in range(0, window + 1):
        for sign in (0, -1, 1):
            if sign == 0 and offset > 0:
                continue
            pdf_p = (leaf_hint - 1) + sign * offset
            if pdf_p < 0 or pdf_p >= doc.page_count:
                continue
            txt = doc[pdf_p].get_text()
            for needle in candidates:
                if needle and needle in txt:
                    return pdf_p, txt.find(needle)
            # Try canonical match (post-compression) on a compressed text
            compressed = re.sub(r"[^A-Z]", "", txt.upper())
            if canon and canon in compressed:
                return pdf_p, -1
    return None, None


def render_page(doc, pdf_p: int) -> Path:
    """Render PDF page to PNG and return path."""
    out = TMP / f"page.png"
    page = doc[pdf_p]
    mat = fitz.Matrix(400 / 72, 400 / 72)
    pix = page.get_pixmap(matrix=mat)
    pix.save(str(out))
    return out


def tesseract(image: Path, lang: str = "spa") -> str:
    out_txt = TMP / "page_out.txt"
    if out_txt.exists():
        out_txt.unlink()
    # Leptonica on macOS misreads absolute paths in some configurations;
    # use cwd=TMP + relative basenames as a workaround.
    subprocess.run(
        ["tesseract", image.name, "page_out", "-l", lang, "--psm", "3"],
        cwd=str(TMP),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return out_txt.read_text(encoding="utf-8") if out_txt.exists() else ""


def adjudicate(tess_text: str, canonical_title: str, mangled_title: str) -> tuple[str, str]:
    """Decide CONFIRM / CONTRADICT / UNCLEAR + supporting snippet.

    Strip parens from titles before searching; OCR may render parens
    content differently across passes.
    """
    canon_bare = re.sub(r"\([^)]*\)", "", canonical_title).strip()
    mangled_bare = re.sub(r"\([^)]*\)", "", mangled_title).strip()

    # First: look for the canonical lemma as a line-start (article opener)
    line_pat = re.compile(
        rf"\b{re.escape(canon_bare)}\b",
        re.IGNORECASE,
    )
    if line_pat.search(tess_text):
        m = line_pat.search(tess_text)
        i = m.start()
        return "CONFIRM", tess_text[max(0, i - 30):i + 150].replace("\n", " | ")

    # Mangled form (means Tesseract reads same OCR-noise as ABBYY)
    mline_pat = re.compile(rf"\b{re.escape(mangled_bare)}\b", re.IGNORECASE)
    if mline_pat.search(tess_text):
        m = mline_pat.search(tess_text)
        i = m.start()
        return "MANGLED-MATCHES-ABBYY", tess_text[max(0, i - 30):i + 150].replace("\n", " | ")

    # Try fuzzy: any line opener that's similar to the canonical
    from rapidfuzz import fuzz
    best_snippet = None
    best_score = 0
    for line in tess_text.split("\n"):
        if not line.strip():
            continue
        m = re.match(r"^\s*([A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ0-9.\s\(\)\-,/]{2,30})", line)
        if not m:
            continue
        cand = m.group(1).strip()
        score = fuzz.ratio(canon_bare.upper(), cand.upper())
        if score > best_score and score >= 70:
            best_score = score
            best_snippet = f"score={score:.0f}: {line[:150]}"
    if best_snippet:
        return "FUZZY", best_snippet

    return "UNCLEAR", ""


def main():
    con = duckdb.connect(str(DB), read_only=True)
    print("Verifying TITLE_FIXES against Tesseract spa re-OCR\n")

    results = []
    for tid, (canonical, reason) in TITLE_FIXES.items():
        row = con.execute(
            "SELECT vol, leaf, title FROM text_entries WHERE id = ?",
            [tid],
        ).fetchone()
        if not row:
            print(f"  id={tid}: text_entry missing, skip")
            continue
        vol, leaf, current_title = row
        # current_title is the cleaned/corrected one. The original mangled
        # is in TITLE_FIXES[tid][1] reason → no, the mangled is the original
        # value before fix. Pull from cleanup notes.
        # The TITLE_FIXES dict only has the canonical+reason; we need to
        # know the mangled form. The current_title in DB == canonical
        # (after apply). The ABBYY mangled form: query chocr_entries on
        # same (vol, leaf).
        chocr_row = con.execute(
            "SELECT title FROM chocr_entries WHERE vol = ? AND leaf = ? LIMIT 1",
            [vol, leaf],
        ).fetchone()
        mangled = chocr_row[0] if chocr_row else current_title

        pdf_path = PROJECT / "data" / "pdf" / f"tomo{vol}.pdf"
        if not pdf_path.exists():
            print(f"  id={tid} v{vol} l{leaf}: PDF not downloaded — skip")
            continue

        doc = fitz.open(str(pdf_path))
        pdf_p, _ = find_page_with_lemma(doc, mangled, leaf, window=8)
        if pdf_p is None:
            # Try canonical form
            pdf_p, _ = find_page_with_lemma(doc, canonical, leaf, window=8)
        if pdf_p is None:
            print(f"  id={tid} v{vol} l{leaf} {current_title}: lemma not found in PDF text — skip")
            doc.close()
            continue

        print(f"  id={tid} v{vol} l{leaf} → PDF page {pdf_p} (Tesseract…)", end=" ", flush=True)
        img = render_page(doc, pdf_p)
        doc.close()
        tess_text = tesseract(img, lang="spa")
        verdict, snippet = adjudicate(tess_text, canonical, mangled)
        print(f"[{verdict}]")
        if snippet:
            print(f"     {snippet[:200]}")
        results.append((tid, vol, leaf, current_title, canonical, mangled, verdict, snippet))

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    from collections import Counter
    counts = Counter(r[6] for r in results)
    for v, n in counts.most_common():
        print(f"  {v:30s} {n}")
    print()
    print("Per-entry:")
    for tid, vol, leaf, ct, can, mng, verdict, snippet in results:
        print(f"  {verdict:30s}  id={tid} v{vol} l{leaf}: \"{can}\"  (ABBYY: \"{mng}\")")


if __name__ == "__main__":
    main()
