"""Descarrega els fitxers OCR i el mapa de pàgines d'un tom del Madoz.

Internet Archive té els 16 toms del Diccionari de Madoz (1845-1850) amb
identificadors `diccionariogeogr01mado` ... `diccionariogeogr16mado`.
Per cada tom necessitem:

- `_chocr.html.gz` (~64 MB): hOCR comprimit amb una caixa delimitadora
  per a cada paraula. L'identificador `id="word_LEAF_INDEX"` permet
  recuperar a quin full ha caigut cada paraula.
- `_page_numbers.json` (~107 KB): mapa `leafNum -> número de pàgina
  printat`. Calibrat amb confiança ~96%.
- `_djvu.txt` (~6 MB): text pla extret (sense pàgines). Útil per a
  cerca ràpida; en aquest projecte el guardem per referència.

Run: python scripts/fetch_volume.py <tom>   (per exemple: 02)
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
        print(f"  [skip] {out.relative_to(PROJECT)} ja existeix")
        return out
    print(f"  [GET]  {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r:
        out.write_bytes(r.read())
    print(f"  [OK]   {out.relative_to(PROJECT)} ({out.stat().st_size/1024/1024:.1f} MB)")
    return out


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Ús: python scripts/fetch_volume.py <vol>  (per ex.: 02)")
    vol = sys.argv[1].zfill(2)
    if vol not in [f"{i:02d}" for i in range(1, 17)]:
        sys.exit(f"Volum invàlid: {vol}. Ha d'estar entre 01 i 16.")
    print(f"=== Tom {vol} ===")
    for kind in FILES:
        fetch(vol, kind)


if __name__ == "__main__":
    main()
