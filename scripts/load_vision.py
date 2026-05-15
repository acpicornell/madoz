"""Load Phase 3 Vision JSON output into the project DuckDB.

Reads every `data/vision/page_<vol>_<leaf>.json` produced by
extract_vision.py and inserts one row per entry into the
`vision_entries` table defined in db/schema.sql. Each run fully
replaces the table, so the JSON files remain the source of truth.

Run: python scripts/load_vision.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
SCHEMA = PROJECT / "db" / "schema.sql"
VISION_DIR = PROJECT / "data" / "vision"


def main() -> None:
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    if not VISION_DIR.exists():
        sys.exit(f"No Vision output dir at {VISION_DIR}.")

    files = sorted(VISION_DIR.glob("page_*.json"))
    if not files:
        sys.exit(f"No page_*.json files under {VISION_DIR}.")
    print(f"Reading {len(files)} page extracts...")

    payload: list[tuple] = []
    for path in files:
        page = json.loads(path.read_text())
        vol = page["vol"]
        leaf = int(page["leaf"])
        page_printed = page.get("page_printed")
        model = page.get("model")
        for e in page.get("entries", []):
            stats = e.get("stats")
            xrefs = e.get("cross_references") or []
            payload.append((
                vol, leaf, page_printed,
                e.get("title") or "",
                e.get("place_type"),
                e.get("island"),
                e.get("judicial_district"),
                e.get("municipality"),
                e.get("description"),
                json.dumps(stats, ensure_ascii=False) if stats else None,
                xrefs,
                e.get("confidence"),
                None,   # chocr_entry_id  — filled by a later matching step
                None,   # madoz_entry_id  — filled by a later matching step
                model,
            ))

    con = duckdb.connect(str(DB))
    con.execute(SCHEMA.read_text())
    con.execute("BEGIN")
    con.execute("DELETE FROM vision_entries")
    con.executemany(
        """INSERT INTO vision_entries
           (vol, leaf, page_printed, title, place_type, island,
            judicial_district, municipality, description, stats,
            cross_references, confidence, chocr_entry_id,
            madoz_entry_id, model)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        payload,
    )
    con.execute("COMMIT")

    n_total, n_high, n_med, n_low = con.execute(
        "SELECT COUNT(*), "
        "       SUM(CASE WHEN confidence='high' THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN confidence='medium' THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN confidence='low' THEN 1 ELSE 0 END) "
        "FROM vision_entries"
    ).fetchone()
    print(f"vision_entries: {n_total} total  "
          f"(high={n_high or 0}  medium={n_med or 0}  low={n_low or 0})")

    print("\n--- Distribution by island ---")
    for r in con.execute(
        "SELECT COALESCE(island,'(unknown)') AS island, COUNT(*) "
        "FROM vision_entries GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {r[0]:15} {r[1]:>5}")

    print("\n--- Top place types ---")
    for r in con.execute(
        "SELECT COALESCE(place_type,'(unknown)') AS pt, COUNT(*) "
        "FROM vision_entries GROUP BY 1 ORDER BY 2 DESC LIMIT 12"
    ).fetchall():
        print(f"  {r[0]:20} {r[1]:>5}")


if __name__ == "__main__":
    main()
