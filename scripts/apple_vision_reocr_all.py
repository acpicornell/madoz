"""Parallel Apple Vision re-OCR over the PDF pages where text_entries
has at least one Balearic article. Mirror of tesseract_reocr_all.py
but using the macOS Vision framework instead of Tesseract.

Outputs `data/applevision/text/tomoNN_pNNNN.txt` per (vol, pdf_page),
one line per recognised text observation (Vision's natural output
unit). The line ordering follows Vision's internal top-down sort.

Apple Vision uses the M-series Neural Engine plus CPU; multi-process
parallelism scales well up to ~8 workers, beyond which the NE
saturates and adding workers doesn't help.

Idempotent: skips text files that already exist. Re-running picks up
where it left off.
"""
from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
PDF_DIR = PROJECT / "data" / "pdf"
OUT_DIR = PROJECT / "data" / "applevision" / "text"
TMP_BASE = Path("/tmp/madoz_vision_reocr")


def _worker_setup(worker_id: int) -> Path:
    work_dir = TMP_BASE / f"worker_{worker_id:02d}"
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def ocr_page(args) -> tuple[str, int, int, float]:
    """Worker fn. args = (vol, pdf_page, dpi).
    Renders the page, runs Apple Vision via pyobjc, writes text output.
    Returns (vol, pdf_page, n_lines, elapsed_seconds)."""
    vol, pdf_p, dpi = args
    t0 = time.time()
    out_txt = OUT_DIR / f"tomo{vol}_p{pdf_p:04d}.txt"
    if out_txt.exists() and out_txt.stat().st_size > 0:
        return vol, pdf_p, 0, 0.0

    work_dir = _worker_setup(os.getpid())

    import fitz
    pdf_path = PDF_DIR / f"tomo{vol}.pdf"
    doc = fitz.open(str(pdf_path))
    page = doc[pdf_p]
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72))
    img_path = work_dir / "page.png"
    pix.save(str(img_path))
    doc.close()

    from Cocoa import NSURL
    from Foundation import NSDictionary
    import Quartz
    import Vision

    url = NSURL.fileURLWithPath_(str(img_path))
    image_source = Quartz.CGImageSourceCreateWithURL(url, None)
    if image_source is None:
        out_txt.write_text("", encoding="utf-8")
        return vol, pdf_p, 0, time.time() - t0
    cg_image = Quartz.CGImageSourceCreateImageAtIndex(image_source, 0, None)
    if cg_image is None:
        out_txt.write_text("", encoding="utf-8")
        return vol, pdf_p, 0, time.time() - t0

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
        cg_image, NSDictionary.dictionary()
    )
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    request.setRecognitionLanguages_(["es-ES"])

    success, _err = handler.performRequests_error_([request], None)
    lines: list[str] = []
    if success:
        for obs in (request.results() or []):
            candidates = obs.topCandidates_(1)
            if candidates:
                lines.append(candidates[0].string())
    text = "\n".join(lines)
    out_txt.write_text(text, encoding="utf-8")
    return vol, pdf_p, len(lines), time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8,
                    help="Concurrent Vision processes. Default 8 "
                    "(Neural Engine saturates beyond this).")
    ap.add_argument("--dpi", type=int, default=400,
                    help="Render DPI. Default 400.")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_BASE.mkdir(parents=True, exist_ok=True)

    # Target page set: (vol, pdf_page) for every distinct (vol, leaf)
    # in text_entries where leaf > 0 (leaf 0 = placeholder for
    # Phase-5-style entries with no chocr counterpart).
    import duckdb
    con = duckdb.connect(str(PROJECT / "db" / "madoz.duckdb"), read_only=True)
    pages = con.execute("""
        SELECT vol, leaf - 1 AS pdf_p FROM text_entries
        WHERE vol IS NOT NULL AND leaf IS NOT NULL AND leaf > 0
        GROUP BY 1, 2 ORDER BY 1, 2
    """).fetchall()
    con.close()

    jobs = []
    skipped = 0
    for vol, pdf_p in pages:
        pdf_path = PDF_DIR / f"tomo{vol}.pdf"
        if not pdf_path.exists():
            continue
        out = OUT_DIR / f"tomo{vol}_p{pdf_p:04d}.txt"
        if out.exists() and out.stat().st_size > 0:
            skipped += 1
            continue
        jobs.append((vol, pdf_p, args.dpi))

    print(f"\nTargets: {len(pages)} pages across "
          f"{len(set(p[0] for p in pages))} volumes")
    print(f"Already done (skipped): {skipped}")
    print(f"Queued: {len(jobs)}")
    print(f"Workers: {args.workers}, DPI: {args.dpi}\n")

    t_start = time.time()
    done = 0
    elapsed_sum = 0.0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(ocr_page, j) for j in jobs]
        for fut in as_completed(futures):
            vol, pdf_p, n_lines, elapsed = fut.result()
            done += 1
            elapsed_sum += elapsed
            if done % 25 == 0 or done == len(jobs):
                wall = time.time() - t_start
                throughput = done / wall if wall > 0 else 0
                eta = (len(jobs) - done) / throughput if throughput > 0 else 0
                print(f"  [{done:4d}/{len(jobs):4d}]  wall {wall/60:.1f}m  "
                      f"avg/page {elapsed_sum/done:.1f}s  "
                      f"{throughput:.1f} p/s  ETA {eta/60:.0f}m  "
                      f"(last v{vol} p{pdf_p:04d} {n_lines} lines)")

    print(f"\nDone in {(time.time() - t_start)/60:.1f} minutes.")
    print(f"Outputs at: {OUT_DIR}")


if __name__ == "__main__":
    main()
