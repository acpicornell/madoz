"""Download the page JPEGs we need for Phase 3 (Vision extraction).

Reads the unique (vol, leaf) pairs from `chocr_entries` and fetches each
corresponding page image from Internet Archive at ~350 dpi (1600 px
wide, ~150 KB per page). Stores under `data/pages/tomo<vol>_leaf<leaf>.jpg`.

Volume 10 uses an alternate IA identifier
(`diccionariogeogr10madouoft`) — handled inline.

Run: python scripts/fetch_pages.py
Output: data/pages/*.jpg (~684 files, ~100 MB total)
"""
from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
PAGES_DIR = PROJECT / "data" / "pages"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15")
SLEEP_BETWEEN = 0.4   # seconds between successful requests (IA is fine here)
MAX_RETRIES = 4

# Internet Archive identifiers. Tom 10 lives under a UofT scan.
IA_IDENT = {f"{i:02d}": f"diccionariogeogr{i:02d}mado" for i in range(1, 17)}
IA_IDENT["10"] = "diccionariogeogr10madouoft"


def url_for(vol: str, leaf: int) -> str:
    ident = IA_IDENT[vol]
    return f"https://archive.org/download/{ident}/page/n{leaf}_w1600.jpg"


def fetch(vol: str, leaf: int, out: Path) -> bool:
    url = url_for(vol, leaf)
    delay = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                data = r.read()
            if not data:
                raise RuntimeError("empty body")
            out.write_bytes(data)
            return True
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as e:
            msg = str(e)
            if attempt == MAX_RETRIES:
                print(f"  [fail] tom{vol} leaf{leaf}: {msg}", file=sys.stderr)
                return False
            print(f"  [retry {attempt}] tom{vol} leaf{leaf}: {msg}; sleep {delay:.1f}s",
                  file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    return False


def main() -> None:
    if not DB.exists():
        sys.exit(f"DB not found at {DB}. Run load_chocr_index.py first.")
    PAGES_DIR.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(DB), read_only=True)
    pairs = con.execute(
        "SELECT DISTINCT vol, leaf FROM chocr_entries ORDER BY vol, leaf"
    ).fetchall()
    print(f"Unique pages needed: {len(pairs)}")

    skipped = downloaded = failed = 0
    for vol, leaf in pairs:
        out = PAGES_DIR / f"tomo{vol}_leaf{leaf}.jpg"
        if out.exists() and out.stat().st_size > 1024:
            skipped += 1
            continue
        ok = fetch(vol, leaf, out)
        if ok:
            downloaded += 1
            print(f"  [ok] tom{vol} leaf{leaf} ({out.stat().st_size/1024:.0f} KB)")
            time.sleep(SLEEP_BETWEEN)
        else:
            failed += 1

    print()
    print(f"Downloaded: {downloaded}")
    print(f"Skipped (already present): {skipped}")
    print(f"Failed: {failed}")


if __name__ == "__main__":
    main()
