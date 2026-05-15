"""Load the chOCR-derived index into the project DuckDB.

Reads:
  - data/index/all.jsonl          (regex-based, source='regex')
  - data/index/from_scrape.jsonl  (recovery imports, source='scrape')

Populates the `chocr_entries` table defined in db/schema.sql. Each run
fully replaces the table contents, so the JSONL files remain the
source of truth for that table.

Run: python scripts/load_chocr_index.py
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
SCHEMA = PROJECT / "db" / "schema.sql"
INDEX_DIR = PROJECT / "data" / "index"


def load_jsonl(path: Path, default_source: str) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            e.setdefault("source", default_source)
            rows.append(e)
    return rows


def main() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB))
    con.execute(SCHEMA.read_text())

    regex_rows = load_jsonl(INDEX_DIR / "all.jsonl", "regex")
    nm_rows = load_jsonl(INDEX_DIR / "from_scrape.jsonl", "scrape")
    print(f"Loaded {len(regex_rows)} regex entries, {len(nm_rows)} scrape imports")

    con.execute("BEGIN")
    con.execute("DELETE FROM chocr_entries")
    # The id column has a default from a sequence; let DuckDB assign it.
    payload = []
    for e in regex_rows + nm_rows:
        payload.append((
            e["vol"],
            int(e["leaf"]),
            e.get("page_printed"),
            e["title"],
            e.get("context"),
            e.get("source") or "regex",
            None,  # madoz_entry_id — populated by a separate match step
            e.get("place_type"),
            e.get("island"),
            e.get("judicial_district"),
            e.get("municipality"),
        ))
    con.executemany(
        """INSERT INTO chocr_entries
           (vol, leaf, page_printed, title, context, source, madoz_entry_id,
            place_type, island, judicial_district, municipality)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        payload,
    )
    con.execute("COMMIT")

    n_total, n_regex, n_nm = con.execute(
        "SELECT COUNT(*), "
        "       SUM(CASE WHEN source='regex' THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN source='scrape' THEN 1 ELSE 0 END) "
        "FROM chocr_entries"
    ).fetchone()
    print(f"chocr_entries: {n_total} total ({n_regex} regex, {n_nm} scrape)")

    print("\n--- Coverage by volume ---")
    for r in con.execute(
        "SELECT vol, COUNT(*) FROM chocr_entries GROUP BY vol ORDER BY vol"
    ).fetchall():
        print(f"  tom{r[0]}: {r[1]:>4}")


if __name__ == "__main__":
    main()
