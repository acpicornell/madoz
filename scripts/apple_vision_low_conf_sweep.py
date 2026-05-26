"""Apple Vision sweep over the same ABBYY low-confidence pages as
scripts/tesseract_low_conf_sweep.py.

Apples-to-apples comparison: same target pages (chocr_entries titles
with OCR mangle markers), same Balearic-strong filter, same dedup
logic against text_entries + chocr_entries. Only the OCR engine differs.

Apple Vision is invoked via the system Vision framework (pyobjc-
framework-Vision). It runs on the Neural Engine and CPU; on M4 Pro
typically ~2-3s per page. No tokens, no network, no API cost.

Output: same shape as the Tesseract sweep — novel candidates + cleaner
reads — so we can diff the two reports directly.
"""
from __future__ import annotations

import re
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import duckdb
import fitz
import objc
from Cocoa import NSURL
from Foundation import NSDictionary
import Quartz
import Vision

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
TMP = Path("/tmp/madoz_vision_sweep")
TMP.mkdir(parents=True, exist_ok=True)

BALEARIC_STRONG = re.compile(
    r"\b(?:Mallorca|Menorca|Iviza|Ibiza|Baleares|Mah[óo]n|Eivissa|"
    r"Palma\s+de\s+Mallorca|Formentera)\b",
    re.IGNORECASE,
)

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
    for off in (0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5):
        pdf_p = (leaf - 1) + off
        if pdf_p < 0 or pdf_p >= doc.page_count:
            continue
        if marker_text in doc[pdf_p].get_text():
            return pdf_p
    return (leaf - 1) if 0 <= leaf - 1 < doc.page_count else None


def apple_vision_ocr(image_path: Path, languages=("es-ES",)) -> str:
    """Run Apple Vision text recognition on an image file. Returns the
    concatenated text (line-separated)."""
    url = NSURL.fileURLWithPath_(str(image_path))
    image_source = Quartz.CGImageSourceCreateWithURL(url, None)
    if image_source is None:
        return ""
    cg_image = Quartz.CGImageSourceCreateImageAtIndex(image_source, 0, None)
    if cg_image is None:
        return ""

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
        cg_image, NSDictionary.dictionary()
    )
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    request.setRecognitionLanguages_(list(languages))

    success, error = handler.performRequests_error_([request], None)
    if not success:
        return ""
    observations = request.results() or []
    lines = []
    for obs in observations:
        # Each observation has .topCandidates(N) -> list of VNRecognizedText
        candidates = obs.topCandidates_(1)
        if candidates:
            lines.append(candidates[0].string())
    return "\n".join(lines)


def render_page(doc, pdf_p: int) -> Path:
    img = TMP / "page.png"
    page = doc[pdf_p]
    pix = page.get_pixmap(matrix=fitz.Matrix(400 / 72, 400 / 72))
    pix.save(str(img))
    return img


def extract_openers_with_bodies(text: str) -> list[tuple[str, str]]:
    matches = list(OPENER_RE.finditer(text))
    out = []
    for i, m in enumerate(matches):
        lemma = m.group(1).strip()
        if not lemma or not re.match(r"^[A-ZÁÉÍÓÚÑÜ]{2,}", lemma):
            continue
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        out.append((lemma, body[:400]))
    return out


def main():
    con = duckdb.connect(str(DB), read_only=True)
    text_by_vol = defaultdict(list)
    for r in con.execute("SELECT vol, title FROM text_entries").fetchall():
        text_by_vol[r[0]].append((r[1], compress(r[1])))
    chocr_by_vol = defaultdict(list)
    for r in con.execute(
        "SELECT vol, title FROM chocr_entries WHERE source='regex'"
    ).fetchall():
        chocr_by_vol[r[0]].append((r[1], compress(r[1])))

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
    cleaner_reads = []
    t_start = time.time()
    for i, ((vol, leaf), mangled_titles) in enumerate(sorted(mangle_targets.items())):
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

        t0 = time.time()
        ocr_text = apple_vision_ocr(img)
        dt = time.time() - t0
        if (i + 1) % 10 == 0:
            print(f"  [{i+1:3d}/{len(mangle_targets)}] last v{vol} l{leaf} took {dt:.1f}s")

        if not ocr_text:
            continue

        openers = extract_openers_with_bodies(ocr_text)
        for lemma, body in openers:
            full = lemma + " " + body
            if not BALEARIC_STRONG.search(full):
                continue
            ck = compress(lemma)
            if len(ck) < 3:
                continue
            in_text, best_text = False, None
            for orig, c in text_by_vol[vol]:
                if not c: continue
                if ck == c or ck in c or c in ck:
                    in_text, best_text = True, orig; break
                if fuzz.ratio(ck, c) >= 75:
                    in_text, best_text = True, orig; break
            in_chocr, best_chocr = False, None
            for orig, c in chocr_by_vol[vol]:
                if not c: continue
                if ck == c or ck in c or c in ck:
                    in_chocr, best_chocr = True, orig; break
                if fuzz.ratio(ck, c) >= 75:
                    in_chocr, best_chocr = True, orig; break

            if in_text:
                if best_text and compress(best_text) != ck and MANGLE_PATTERNS.search(best_text):
                    cleaner_reads.append((vol, leaf, lemma, best_text, body[:80]))
                continue
            if in_chocr:
                if best_chocr and MANGLE_PATTERNS.search(best_chocr):
                    cleaner_reads.append((vol, leaf, lemma, best_chocr, body[:80]))
                continue
            novel.append((vol, leaf, pdf_p, lemma, body[:200]))

    total_time = time.time() - t_start
    print(f"\nElapsed: {total_time/60:.1f} min ({total_time/len(mangle_targets):.1f}s/page avg)")
    print(f"\nNovel Balearic openers (Apple Vision found, ABBYY missed): {len(novel)}")
    for v, l, pp, lemma, body in novel:
        print(f"  v{v} l{l} pdf{pp}  \"{lemma[:30]:30s}\"")
        print(f"    body: {body[:150]}")
    print(f"\nCleaner Apple Vision reads for known articles: {len(cleaner_reads)}")
    for v, l, lemma, was, body in cleaner_reads[:40]:
        print(f"  v{v} l{l}: \"{was}\"  →  Apple Vision: \"{lemma}\"")


if __name__ == "__main__":
    main()
