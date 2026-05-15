"""Recover Madoz entries from the source site that are mis-categorized.

diccionariomadoz.com hosts a few Balearic articles under the wrong WP
category (e.g. ALCUDIA → /Alava/, MARRATXI →
/Las-Palmas-de-Gran-Canaria/), so the `categories=7` filter used by
scrape_madoz.py misses them. This script fetches each known slug
through the WP REST API and writes them to `data/madoz/extras.jsonl`.
The next run of scrape_madoz.py (fresh or --from-cache) automatically
merges them with posts.jsonl.

Run: python scripts/scrape_madoz_extras.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Reuse the polite HTTP layer from scrape_madoz.py (neutral UA, backoff,
# inter-request sleep).
sys.path.insert(0, str(Path(__file__).parent))
from scrape_madoz import (  # noqa: E402
    EXTRAS_JSONL, RAW_DIR, SLEEP_BETWEEN, get_json,
)

API = "https://www.diccionariomadoz.com/wp-json/wp/v2"

# Slugs identified as Balearic entries published under the wrong
# category. Each corresponds to a municipality article that the
# `categories=7` filter would miss.
EXTRA_SLUGS = [
    "alcudia",   # ciudad, Mallorca → published under /Alava/
    "marratxi",  # villa, Mallorca → published under /Las-Palmas-de-Gran-Canaria/
]


def fetch_by_slug(slug: str) -> list[dict]:
    """Return posts matching this exact slug (typically 0 or 1)."""
    url = f"{API}/posts?slug={slug}&per_page=10"
    data, _ = get_json(url)
    return data if isinstance(data, list) else []


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for slug in EXTRA_SLUGS:
        print(f"Fetching slug={slug!r}...")
        posts = fetch_by_slug(slug)
        if not posts:
            print(f"  ! no posts found for slug {slug!r}")
            continue
        for p in posts:
            title = (p.get("title") or {}).get("rendered") or ""
            print(f"  + id={p['id']} title={title!r} link={p.get('link')}")
            out.append(p)
        time.sleep(SLEEP_BETWEEN)

    with EXTRAS_JSONL.open("w") as f:
        for p in out:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"\nWrote {len(out)} extras to {EXTRAS_JSONL}")
    print("Now run: python scripts/scrape_madoz.py --from-cache")


if __name__ == "__main__":
    main()
