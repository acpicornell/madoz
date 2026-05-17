"""One-shot migration: add ``description_raw`` to text_entries and back
fill it from the current ``description`` so future normalizations can
keep the LLM's first-pass extraction alongside the cleaned version.

Effects:
- Live DB: ALTER TABLE text_entries ADD COLUMN description_raw TEXT;
  then UPDATE text_entries SET description_raw = description WHERE
  description_raw IS NULL;
- Every data/text/page_<vol>_<leaf>.json gets a ``description_raw``
  field appended to each entry (= the current ``description``) if the
  field is missing.

After this runs, normalize_medium_conf.py (and similar) only touch
``description``. The raw never gets overwritten by a fix script, and
load_text.py reads description_raw from the JSON so re-loads preserve
it.

Idempotent: running again is a no-op (column exists, raw already set).

  python scripts/migrate_add_description_raw.py            # dry run
  python scripts/migrate_add_description_raw.py --apply
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
TEXT_DIR = PROJECT / "data" / "text"


def column_exists(con: duckdb.DuckDBPyConnection, table: str, column: str) -> bool:
    rows = con.execute(
        f"SELECT 1 FROM information_schema.columns WHERE table_name=? AND column_name=?",
        [table, column],
    ).fetchall()
    return bool(rows)


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    has_col = column_exists(con, "text_entries", "description_raw")
    print(f"DB: description_raw column present = {has_col}")
    if has_col:
        unset = con.execute(
            "SELECT COUNT(*) FROM text_entries WHERE description_raw IS NULL"
        ).fetchone()[0]
        print(f"  rows with description_raw NULL: {unset}")
    else:
        unset = con.execute("SELECT COUNT(*) FROM text_entries").fetchone()[0]
        print(f"  (column missing — would backfill {unset} rows)")

    # Plan JSON edits
    json_paths = sorted(TEXT_DIR.glob("page_*.json"))
    json_to_patch: list[Path] = []
    for p in json_paths:
        data = json.loads(p.read_text(encoding="utf-8"))
        if any("description_raw" not in e for e in data.get("entries", [])):
            json_to_patch.append(p)
    print(f"JSON: {len(json_to_patch)} of {len(json_paths)} files need backfill")

    if not apply:
        print("\nDRY RUN — pass --apply to commit.")
        return

    # 1. Add column if missing
    if not has_col:
        con.execute("ALTER TABLE text_entries ADD COLUMN description_raw TEXT")
        print("  ✓ added column")

    # 2. Backfill DB rows
    n_back = con.execute(
        "UPDATE text_entries SET description_raw = description "
        "WHERE description_raw IS NULL"
    ).fetchone()
    print(f"  ✓ backfilled DB (description_raw <- description on NULL rows)")

    # 3. Backfill JSON files
    for p in json_to_patch:
        data = json.loads(p.read_text(encoding="utf-8"))
        changed = False
        for e in data.get("entries", []):
            if "description_raw" not in e:
                e["description_raw"] = e.get("description")
                changed = True
        if changed:
            p.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    print(f"  ✓ backfilled {len(json_to_patch)} JSON files")

    print("\nDone.")


if __name__ == "__main__":
    main()
