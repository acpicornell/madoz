"""Download the OCR files and page map for a Madoz volume.

Internet Archive hosts the 16 volumes of Madoz's Diccionario (1845-1850)
under identifiers `diccionariogeogr01mado` ... `diccionariogeogr16mado`.
For each volume we need:

- `_chocr.html.gz` (~64 MB): compressed hOCR with a bounding box per
  word. The `id="word_LEAF_INDEX"` lets us recover which leaf each
  word belongs to.
- `_page_numbers.json` (~107 KB): map `leafNum -> printed page number`.
  Calibrated with ~96% confidence.
- `_djvu.txt` (~6 MB): plain text (no pagination). Useful for quick
  grep; kept for reference.

Run: python scripts/fetch_volume.py <vol>   (e.g. 02)
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DATA = PROJECT / "data"

UA = "Mozilla/5.0 (research)"
BASE = "https://archive.org/download/diccionariogeogr{vol}mado"
FILES = {
    "chocr": ("diccionariogeogr{vol}mado_chocr.html.gz", DATA / "chocr"),
    "page_numbers": ("diccionariogeogr{vol}mado_page_numbers.json",
                     DATA / "page_numbers"),
    "txt_djvu": ("diccionariogeogr{vol}mado_djvu.txt", DATA / "txt_djvu"),
}


def fetch(vol: str, kind: str) -> Path:
    fname_remote, out_dir = FILES[kind]
    url = f"{BASE.format(vol=vol)}/{fname_remote.format(vol=vol)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(fname_remote).suffix
    if fname_remote.endswith(".html.gz"):
        ext = ".html.gz"
    elif fname_remote.endswith(".json"):
        ext = ".json"
    elif fname_remote.endswith(".txt"):
        ext = ".txt"
    out = out_dir / f"tomo{vol}{ext}"
    if out.exists() and out.stat().st_size > 0:
        print(f"  [skip] {out.relative_to(PROJECT)} already exists")
        return out
    print(f"  [GET]  {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r:
        out.write_bytes(r.read())
    print(f"  [OK]   {out.relative_to(PROJECT)} ({out.stat().st_size/1024/1024:.1f} MB)")
    return out


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: python scripts/fetch_volume.py <vol>  (e.g. 02)")
    vol = sys.argv[1].zfill(2)
    if vol not in [f"{i:02d}" for i in range(1, 17)]:
        sys.exit(f"Invalid volume: {vol}. Must be between 01 and 16.")
    print(f"=== Volume {vol} ===")
    for kind in FILES:
        fetch(vol, kind)


if __name__ == "__main__":
    main()
