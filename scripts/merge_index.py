"""Merge the 16 per-volume index JSONL files into one deduplicated file.

Reads data/index/tomo<vol>.jsonl for every volume present, deduplicates
by (vol, leaf, title.upper()), and writes data/index/all.jsonl. Prints a
per-volume summary (entries, pages with hits, sample titles).

Run: python scripts/merge_index.py
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
INDEX_DIR = PROJECT / "data" / "index"


def main() -> None:
    files = sorted(INDEX_DIR.glob("tomo*.jsonl"))
    if not files:
        raise SystemExit(f"No tomo*.jsonl files found under {INDEX_DIR}")

    all_entries: list[dict] = []
    per_tom: Counter = Counter()
    for path in files:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                e = json.loads(line)
                all_entries.append(e)
                per_tom[e["vol"]] += 1

    seen: set[tuple] = set()
    unique: list[dict] = []
    for e in all_entries:
        key = (e["vol"], e["leaf"], e["title"].upper())
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)

    out = INDEX_DIR / "all.jsonl"
    with out.open("w") as f:
        for e in unique:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"Volumes read: {len(files)}")
    for vol in sorted(per_tom):
        print(f"  Volume {vol}: {per_tom[vol]:>4} entries")
    print(f"\nTotal raw: {len(all_entries)}")
    print(f"Total deduplicated: {len(unique)}")
    print(f"Wrote: {out.relative_to(PROJECT)}")


if __name__ == "__main__":
    main()
