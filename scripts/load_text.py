"""Load Phase 3 text-pass JSON output into the project DuckDB.

Reads every `data/text/page_<vol>_<leaf>.json` produced by
extract_text.py (API path) or hand-written from chocr by the assistant
under Claude Code (Max path), and inserts one row per entry into the
`text_entries` table defined in db/schema.sql.

Each run fully replaces the table, so the JSON files remain the source
of truth. Re-run after every new batch of leaves.

Run: python scripts/load_text.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
SCHEMA = PROJECT / "db" / "schema.sql"
TEXT_DIR = PROJECT / "data" / "text"


def main() -> None:
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    if not TEXT_DIR.exists():
        sys.exit(f"No text output dir at {TEXT_DIR}.")

    files = sorted(TEXT_DIR.glob("page_*.json"))
    if not files:
        sys.exit(f"No page_*.json files under {TEXT_DIR}.")
    print(f"Reading {len(files)} leaf extracts...")

    payload: list[tuple] = []
    for path in files:
        page = json.loads(path.read_text())
        vol = page["vol"]
        leaf = int(page["leaf"])
        page_printed = page.get("page_printed")
        model = page.get("model")
        window = page.get("window")
        note = page.get("note")
        source_file = str(path.relative_to(PROJECT))
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
                window,
                model,
                source_file,
                note,
                None,  # chocr_entry_id — back-filled by a later matching step
                None,  # madoz_entry_id — back-filled by a later matching step
            ))

    con = duckdb.connect(str(DB))
    con.execute(SCHEMA.read_text())
    con.execute("BEGIN")
    con.execute("DELETE FROM text_entries")
    con.executemany(
        """INSERT INTO text_entries
           (vol, leaf, page_printed, title, place_type, island,
            judicial_district, municipality, description, stats,
            cross_references, confidence, window_size, model,
            source_file, note, chocr_entry_id, madoz_entry_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        payload,
    )
    con.execute("COMMIT")

    n_total, n_high, n_med, n_low = con.execute(
        "SELECT COUNT(*), "
        "       SUM(CASE WHEN confidence='high' THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN confidence='medium' THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN confidence='low' THEN 1 ELSE 0 END) "
        "FROM text_entries"
    ).fetchone()
    print(f"text_entries: {n_total} total  "
          f"(high={n_high or 0}  medium={n_med or 0}  low={n_low or 0})")

    print("\n--- Coverage by leaf ---")
    n_leaves = con.execute(
        "SELECT COUNT(DISTINCT (vol, leaf)) FROM text_entries"
    ).fetchone()[0]
    print(f"  leaves processed: {n_leaves}")

    print("\n--- Distribution by island ---")
    for r in con.execute(
        "SELECT COALESCE(island,'(unknown)') AS island, COUNT(*) "
        "FROM text_entries GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {r[0]:15} {r[1]:>5}")

    print("\n--- Top place types ---")
    for r in con.execute(
        "SELECT COALESCE(place_type,'(unknown)') AS pt, COUNT(*) "
        "FROM text_entries GROUP BY 1 ORDER BY 2 DESC LIMIT 12"
    ).fetchall():
        print(f"  {r[0]:25} {r[1]:>5}")

    print("\n--- Entries that have full stats ---")
    n_with_stats = con.execute(
        "SELECT COUNT(*) FROM text_entries "
        "WHERE stats IS NOT NULL AND json_extract(stats, '$.vecinos') IS NOT NULL"
    ).fetchone()[0]
    print(f"  with vecinos parsed: {n_with_stats}")


if __name__ == "__main__":
    main()
