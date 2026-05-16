"""Fill NULL island fields when the description explicitly cites a
Balearic island ("isla de Mallorca", "isla de Menorca", etc.).

The LLM extractor sometimes omits the island field even when the text
plainly states it. This script is a deterministic completer over what
already lives in the description.

Run AFTER load_text.py. Idempotent.

  python scripts/fill_missing_islands.py            # dry run
  python scripts/fill_missing_islands.py --apply    # commit
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

ISLAND_RE = re.compile(
    r"isla\s+(?:de\s+|del\s+|de\s*l\s*)?(mallorca|menorca|ibiza|formentera|cabrera)",
    re.IGNORECASE,
)


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    rows = con.execute(
        "SELECT id, title, description, source_file FROM text_entries "
        "WHERE island IS NULL"
    ).fetchall()

    plan: list[tuple[int, str, str, str]] = []
    for tid, title, desc, src in rows:
        if not desc:
            continue
        m = ISLAND_RE.search(desc)
        if m:
            plan.append((tid, title, m.group(1).capitalize(), src))

    print(f"{len(plan)} NULL-island entries to fill:")
    for tid, title, isl, _src in plan:
        print(f"  {tid:5} {title[:38]:<38} → {isl}")

    if not apply:
        if plan:
            print("\nDRY RUN — pass --apply to commit.")
        return

    for tid, title, isl, src in plan:
        path = PROJECT / src
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for e in data.get("entries", []):
                if e.get("title") == title and not e.get("island"):
                    e["island"] = isl
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        con.execute("UPDATE text_entries SET island=? WHERE id=?", [isl, tid])
    print(f"\nFilled {len(plan)} rows.")


if __name__ == "__main__":
    main()
