"""C+: Tesseract re-OCR sweep over ABBYY low-confidence pages.

Targets pages where chocr_entries shows OCR mangling (digit inside
lemma, special junk chars, mid-word lowercase, etc.). Renders each
page with PyMuPDF, runs Tesseract 5 (spa), extracts every candidate
article opener from the Tesseract output, and reports those that:
  (a) appear to be Balearic (body contains a strong Balearic token), and
  (b) don't fuzzy-match any text_entries title on the same volume, and
  (c) don't fuzzy-match any chocr_entries title on the same volume.

These are articles ABBYY likely dropped that Tesseract recovers.
"""
from __future__ import annotations

import re
import subprocess
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import duckdb
import fitz

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
TMP = Path("/tmp/madoz_tess_sweep")
TMP.mkdir(parents=True, exist_ok=True)

BALEARIC_STRONG = re.compile(
    r"\b(?:Mallorca|Menorca|Iviza|Ibiza|Baleares|Mah[óo]n|Eivissa|"
    r"Palma\s+de\s+Mallorca|Formentera)\b",
    re.IGNORECASE,
)

# Permissive opener: lemma is at least 2 capital letters, allowing
# accented and parenthesised qualifiers; followed by a Madoz separator
# (`:`, `.`, `,`, `;`).
OPENER_RE = re.compile(
    r"^\s*([A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ.\s\(\)\-,/]{2,60}?)\s*[:.,;]\s*[a-záéíóúñ]",
    re.MULTILINE,
)

MANGLE_PATTERNS = re.compile(
    r"(\d|[¡¿|\\/`\"]|[A-Z]ll?[A-Z]|^[A-Z]{1,3}\s+[A-Z]|[A-Z]{2,}I{2,})"
)


def compress(s: str) -> str:
    if not s:
        return ""
    s = s.upper()
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn")
    return re.sub(r"[^A-Z0-9]", "", s)


def find_pdf_page_for_leaf(doc, vol: str, leaf: int, marker_text: str) -> int | None:
    """Find the PDF page index that contains a given marker (typically
    the mangled ABBYY title) near the expected leaf. PDF index ≈ leaf−1
    but offsets vary."""
    # Try ±5 window
    for off in (0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5):
        pdf_p = (leaf - 1) + off
        if pdf_p < 0 or pdf_p >= doc.page_count:
            continue
        if marker_text in doc[pdf_p].get_text():
            return pdf_p
    # Fall back: just use leaf−1
    return (leaf - 1) if 0 <= leaf - 1 < doc.page_count else None


def tesseract(image_path: Path, lang: str = "spa") -> str:
    out_txt = TMP / "out.txt"
    if out_txt.exists():
        out_txt.unlink()
    subprocess.run(
        ["tesseract", image_path.name, "out", "-l", lang, "--psm", "3"],
        cwd=str(TMP),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return out_txt.read_text(encoding="utf-8") if out_txt.exists() else ""


def render_page(doc, pdf_p: int) -> Path:
    img = TMP / "page.png"
    page = doc[pdf_p]
    pix = page.get_pixmap(matrix=fitz.Matrix(400 / 72, 400 / 72))
    pix.save(str(img))
    return img


def extract_openers_with_bodies(tess_text: str) -> list[tuple[str, str]]:
    """Find every opener line, then collect its body up to the next opener.
    Returns list of (lemma, body_chars)."""
    # Find all opener positions
    matches = list(OPENER_RE.finditer(tess_text))
    out = []
    for i, m in enumerate(matches):
        lemma = m.group(1).strip()
        if not lemma or not re.match(r"^[A-ZÁÉÍÓÚÑÜ]{2,}", lemma):
            continue
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(tess_text)
        body = tess_text[body_start:body_end].strip()
        out.append((lemma, body[:400]))
    return out


def main():
    con = duckdb.connect(str(DB), read_only=True)
    # Build per-volume indexes of existing titles
    text_by_vol = defaultdict(list)
    for r in con.execute("SELECT vol, title FROM text_entries").fetchall():
        text_by_vol[r[0]].append((r[1], compress(r[1])))
    chocr_by_vol = defaultdict(list)
    for r in con.execute(
        "SELECT vol, title FROM chocr_entries WHERE source='regex'"
    ).fetchall():
        chocr_by_vol[r[0]].append((r[1], compress(r[1])))

    # Find target pages
    mangle_targets = defaultdict(list)
    for r in con.execute("""SELECT id, vol, leaf, title FROM chocr_entries
                             WHERE source='regex'""").fetchall():
        cid, vol, leaf, title = r
        if MANGLE_PATTERNS.search(title):
            mangle_targets[(vol, leaf)].append(title)

    print(f"Target pages: {len(mangle_targets)} across "
          f"{len(set(k[0] for k in mangle_targets))} volumes")

    from rapidfuzz import fuzz
    novel = []
    confirmed_mangle_fixes = []
    for (vol, leaf), mangled_titles in sorted(mangle_targets.items()):
        pdf_path = PROJECT / "data" / "pdf" / f"tomo{vol}.pdf"
        if not pdf_path.exists():
            continue
        try:
            doc = fitz.open(str(pdf_path))
            pdf_p = find_pdf_page_for_leaf(doc, vol, leaf, mangled_titles[0])
            if pdf_p is None:
                doc.close()
                continue
            img = render_page(doc, pdf_p)
            doc.close()
        except Exception as e:
            print(f"  v{vol} l{leaf}: render fail ({e})")
            continue

        tess_text = tesseract(img)
        if not tess_text:
            continue
        openers = extract_openers_with_bodies(tess_text)
        # For each opener, check Balearic body + DB coverage
        for lemma, body in openers:
            full = lemma + " " + body
            if not BALEARIC_STRONG.search(full):
                continue
            ck = compress(lemma)
            if len(ck) < 3:
                continue
            # Check text_entries on this volume
            in_text = False
            best_text = None
            for orig, c in text_by_vol[vol]:
                if not c:
                    continue
                if ck == c or ck in c or c in ck:
                    in_text = True; best_text = orig; break
                if fuzz.ratio(ck, c) >= 75:
                    in_text = True; best_text = orig; break
            in_chocr = False
            best_chocr = None
            for orig, c in chocr_by_vol[vol]:
                if not c:
                    continue
                if ck == c or ck in c or c in ck:
                    in_chocr = True; best_chocr = orig; break
                if fuzz.ratio(ck, c) >= 75:
                    in_chocr = True; best_chocr = orig; break

            if in_text:
                # Existing — check if our text title is mangled and Tesseract reads cleaner
                if best_text and compress(best_text) != ck and MANGLE_PATTERNS.search(best_text):
                    confirmed_mangle_fixes.append((vol, leaf, lemma, best_text, body[:80]))
                continue
            if in_chocr:
                # In chocr but not text → already a Phase-4 candidate we may have promoted
                # (or rejected); flag if title differs (cleaner Tesseract reading)
                if best_chocr and MANGLE_PATTERNS.search(best_chocr):
                    confirmed_mangle_fixes.append((vol, leaf, lemma, best_chocr, body[:80]))
                continue
            # Truly novel
            novel.append((vol, leaf, pdf_p, lemma, body[:200]))

    print(f"\nNovel Balearic openers (Tesseract found, ABBYY missed): {len(novel)}")
    for v, l, pp, lemma, body in novel:
        print(f"  v{v} l{l} pdf{pp}  \"{lemma[:30]:30s}\"")
        print(f"    body: {body[:150]}")
    print(f"\nCleaner Tesseract reads for already-known articles: {len(confirmed_mangle_fixes)}")
    for v, l, lemma, was, body in confirmed_mangle_fixes[:40]:
        print(f"  v{v} l{l}: \"{was}\"  →  Tesseract: \"{lemma}\"")


if __name__ == "__main__":
    main()
