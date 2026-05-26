"""Parallel Tesseract re-OCR of all 16 Madoz volumes.

Per-worker pipeline: open PDF → render page → Tesseract spa → text file.
Images are written to a per-worker temp dir and overwritten each page
(no accumulation; total disk impact is just the text outputs at the end,
~50 MB for the full corpus).

Designed for Apple Silicon (M-series). Defaults to 10 parallel workers,
matching the M4 Pro's 10 Performance cores. Tested layout: 300 dpi for
fast turnaround with no quality loss for text recognition.

Usage:
  python scripts/tesseract_reocr_all.py                      # all 16 vols
  python scripts/tesseract_reocr_all.py --vol 02             # one vol
  python scripts/tesseract_reocr_all.py --vol 02 --workers 6 # custom
  python scripts/tesseract_reocr_all.py --dpi 400            # higher dpi

Idempotent: skips text files that already exist. Re-running picks up
where it left off; safe to interrupt with Ctrl-C.

Output: data/tesseract/text/tomo<vol>_p<pdf_page_4digit>.txt
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
PDF_DIR = PROJECT / "data" / "pdf"
OUT_DIR = PROJECT / "data" / "tesseract" / "text"
TMP_BASE = Path("/tmp/madoz_reocr")


def _worker_setup(worker_id: int) -> Path:
    work_dir = TMP_BASE / f"worker_{worker_id:02d}"
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def ocr_page(args) -> tuple[str, int, int, float]:
    """Worker fn. args = (vol, pdf_page, dpi, lang)
    Returns (vol, pdf_page, text_len, elapsed_seconds)."""
    vol, pdf_p, dpi, lang = args
    t0 = time.time()
    out_txt = OUT_DIR / f"tomo{vol}_p{pdf_p:04d}.txt"
    if out_txt.exists() and out_txt.stat().st_size > 0:
        return vol, pdf_p, out_txt.stat().st_size, 0.0  # skip

    # pid-based worker id (multiprocessing assigns unique pids per worker)
    work_dir = _worker_setup(os.getpid())

    # Render
    import fitz
    pdf_path = PDF_DIR / f"tomo{vol}.pdf"
    doc = fitz.open(str(pdf_path))
    page = doc[pdf_p]
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72))
    img_path = work_dir / "page.png"
    pix.save(str(img_path))
    doc.close()

    # OCR — cwd workaround for the macOS Leptonica absolute-path bug
    proc = subprocess.run(
        ["tesseract", "page.png", "stdout", "-l", lang, "--psm", "3"],
        cwd=str(work_dir),
        capture_output=True,
    )
    text = proc.stdout.decode("utf-8", errors="replace")
    out_txt.write_text(text, encoding="utf-8")
    return vol, pdf_p, len(text), time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vol", help="Process only this volume (e.g. '02').")
    ap.add_argument("--workers", type=int, default=10,
                    help="Concurrent Tesseract processes. Default 10 (M4 Pro P-cores).")
    ap.add_argument("--dpi", type=int, default=300,
                    help="Render DPI. Default 300 (good balance speed/quality).")
    ap.add_argument("--lang", default="spa",
                    help="Tesseract language model. Default 'spa' (modern Spanish).")
    ap.add_argument("--start", type=int, default=0,
                    help="Start PDF page index (0-based).")
    ap.add_argument("--end", type=int,
                    help="End PDF page index (exclusive). Default: last page.")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_BASE.mkdir(parents=True, exist_ok=True)

    if args.vol:
        vols = [args.vol]
    else:
        vols = [f"{i:02d}" for i in range(1, 17)]

    # Build the full list of (vol, pdf_page) to process
    import fitz
    jobs: list[tuple[str, int, int, str]] = []
    skipped = 0
    for v in vols:
        pdf_path = PDF_DIR / f"tomo{v}.pdf"
        if not pdf_path.exists():
            print(f"  SKIP v{v}: PDF not downloaded ({pdf_path})")
            continue
        doc = fitz.open(str(pdf_path))
        n_pages = doc.page_count
        doc.close()
        end = args.end if args.end is not None else n_pages
        for pdf_p in range(args.start, min(end, n_pages)):
            out_txt = OUT_DIR / f"tomo{v}_p{pdf_p:04d}.txt"
            if out_txt.exists() and out_txt.stat().st_size > 0:
                skipped += 1
                continue
            jobs.append((v, pdf_p, args.dpi, args.lang))
    if skipped:
        print(f"  (skipping {skipped} pages already done)")

    print(f"\nQueued {len(jobs)} pages across {len(set(j[0] for j in jobs))} volumes")
    print(f"Workers: {args.workers}, DPI: {args.dpi}, lang: {args.lang}\n")

    t_start = time.time()
    done = 0
    elapsed_sum = 0.0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(ocr_page, j) for j in jobs]
        for fut in as_completed(futures):
            vol, pdf_p, sz, elapsed = fut.result()
            done += 1
            elapsed_sum += elapsed
            if done % 50 == 0 or done == len(jobs):
                wall = time.time() - t_start
                throughput = done / wall if wall > 0 else 0
                eta = (len(jobs) - done) / throughput if throughput > 0 else 0
                avg_proc = elapsed_sum / done
                print(f"  [{done:5d}/{len(jobs):5d}]  wall {wall/60:.1f}m  "
                      f"avg/page {avg_proc:.1f}s  "
                      f"throughput {throughput:.1f} p/s  "
                      f"ETA {eta/60:.0f}m  "
                      f"(last: v{vol} p{pdf_p:04d}, {sz} chars)")

    print(f"\nDone in {(time.time() - t_start)/60:.1f} minutes.")
    print(f"Outputs at: {OUT_DIR}")


if __name__ == "__main__":
    main()
