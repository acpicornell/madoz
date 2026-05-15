"""Fusiona els 16 JSONL d'índex en un sol fitxer deduplicat.

Llegeix data/index/tomo<vol>.jsonl per a tots els toms presents, deduplica
per (vol, leaf, title.upper()) i escriu data/index/all.jsonl. També
imprimeix un resum per tom (entrades, pàgines amb hit, primers títols).

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
        raise SystemExit(f"No hi ha cap tomo*.jsonl a {INDEX_DIR}")

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

    print(f"Tomos llegits: {len(files)}")
    for vol in sorted(per_tom):
        print(f"  Tom {vol}: {per_tom[vol]:>4} entrades")
    print(f"\nTotal en cru: {len(all_entries)}")
    print(f"Total deduplicat: {len(unique)}")
    print(f"Escrit: {out.relative_to(PROJECT)}")


if __name__ == "__main__":
    main()
