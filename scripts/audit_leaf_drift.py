"""Detect entries whose (leaf, page_printed) coordinates are
inconsistent with their volume's typical offset.

Internet Archive scans index leaves sequentially while Madoz numbers
the printed pages independently. For a clean alignment, ``leaf -
page_printed`` is roughly constant within a volume (the offset comes
from front matter — cover, half-title, prologue, indexes — that pads
the scan before printed page 1). A row whose offset deviates from the
volume median is likely mis-indexed: either the chocr regex picked
the wrong leaf, or the printed page number was misread.

Two categories surfaced:

- ``drift``: page_printed is set but its offset is more than 5 pages
  off the volume median. The entry probably points at the wrong
  scanned leaf, and the IA facsimile link will land on the wrong
  page.

- ``unknown_page``: page_printed is missing or '?'. We can't verify
  drift but flag for manual review.

Run:
  python scripts/audit_leaf_drift.py            # report
  python scripts/audit_leaf_drift.py --threshold 8  # custom drift threshold
"""
from __future__ import annotations
import argparse
import statistics
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=int, default=5,
                        help="flag entries whose offset deviates more "
                             "than N pages from the volume median")
    args = parser.parse_args()
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=True)

    rows = con.execute(
        "SELECT id, vol, leaf, page_printed, title, confidence "
        "FROM text_entries ORDER BY vol, leaf, id"
    ).fetchall()

    # Group by vol
    by_vol: dict[str, list] = {}
    for row in rows:
        by_vol.setdefault(row[1], []).append(row)

    print(f"{'vol':>4} {'entries':>8} {'with_pp':>8} {'offsets':>40} {'drift':>5} {'unknown':>7}")
    print("-" * 76)
    drift_rows: list = []
    unknown_rows: list = []
    NEIGHBOR_WINDOW = 5  # how many nearest entries to compare against
    for vol, entries in sorted(by_vol.items()):
        # Build list of (leaf, pp_int, offset, id, title, conf), sorted by leaf
        items = []
        for (tid, _v, leaf, pp, title, conf) in entries:
            if pp is None or pp == "?":
                unknown_rows.append((tid, vol, leaf, pp, title, conf))
                continue
            try:
                pp_int = int(str(pp).strip())
            except ValueError:
                unknown_rows.append((tid, vol, leaf, pp, title, conf))
                continue
            items.append((leaf, pp_int, leaf - pp_int, tid, title, conf))
        items.sort()
        if not items:
            continue
        # For each item, compare its offset against the median of its
        # NEIGHBOR_WINDOW nearest neighbours (by leaf order).
        offsets_summary = sorted(set(off for _, _, off, *_ in items))
        vol_drift = 0
        for i, (leaf, pp_int, off, tid, title, conf) in enumerate(items):
            lo = max(0, i - NEIGHBOR_WINDOW)
            hi = min(len(items), i + NEIGHBOR_WINDOW + 1)
            neighbours = items[lo:hi]
            neighbour_offsets = [n[2] for n in neighbours if n[3] != tid]
            if not neighbour_offsets:
                continue
            neighbour_med = statistics.median(neighbour_offsets)
            if abs(off - neighbour_med) > args.threshold:
                drift_rows.append((tid, vol, leaf, pp_int, off, neighbour_med,
                                   abs(off - neighbour_med), title, conf))
                vol_drift += 1
        vol_unknown = sum(1 for r in entries if r[3] in (None, "?"))
        print(f"  {vol:>4} {len(entries):>8} {len(items):>8} "
              f"{str(offsets_summary)[:38]:>40} {vol_drift:>5} {vol_unknown:>7}")

    print(f"\n=== {len(drift_rows)} entries flagged as drift "
          f"(|offset - median| > {args.threshold}) ===\n")
    for (tid, vol, leaf, pp, off, med, dev, title, conf) in sorted(
        drift_rows, key=lambda r: -r[6]
    ):
        print(f"  id={tid:5} {title!r:42} vol={vol} leaf={leaf:4} "
              f"page={pp:4} off={off:>4} (med {med}, dev {dev}) [{conf}]")

    print(f"\n=== {len(unknown_rows)} entries with unknown page_printed ===\n")
    for (tid, vol, leaf, pp, title, conf) in unknown_rows:
        print(f"  id={tid:5} {title!r:42} vol={vol} leaf={leaf:4} "
              f"pp={pp!r} [{conf}]")


if __name__ == "__main__":
    main()
