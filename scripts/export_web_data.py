"""Export text_entries to a single web/data.json — consumed directly by
the static web (no DuckDB-WASM).

Run after any data refresh:
  python scripts/export_web_data.py
"""
from __future__ import annotations
import json
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
OUT = PROJECT / "web" / "data.json"


def main() -> None:
    con = duckdb.connect(str(DB), read_only=True)
    rows = con.execute(
        """
        SELECT id, vol, leaf, page_printed, title,
               place_type, island, judicial_district, municipality,
               description, stats, cross_references, confidence,
               note
        FROM text_entries
        ORDER BY title
        """
    ).fetchall()
    cols = [
        "id", "vol", "leaf", "page_printed", "title",
        "place_type", "island", "judicial_district", "municipality",
        "description", "stats", "cross_references", "confidence",
        "note",
    ]

    entries = []
    for row in rows:
        d = dict(zip(cols, row))
        if isinstance(d["stats"], str) and d["stats"]:
            try:
                d["stats"] = json.loads(d["stats"])
            except json.JSONDecodeError:
                d["stats"] = None
        if d["cross_references"] is None:
            d["cross_references"] = []
        # Facsimile link to the Internet Archive scan. Page-level (one
        # leaf can hold several entries), not paragraph-level — the UI
        # makes that clear.
        d["ia_url"] = (
            f"https://archive.org/details/diccionariogeogr{d['vol']}mado"
            f"/page/n{d['leaf']}/mode/2up"
        )
        # drop empty/falsy fields to keep the JSON small
        for k in list(d):
            if d[k] in (None, "", []):
                del d[k]
        entries.append(d)

    payload = {
        "generated_with": "scripts/export_web_data.py",
        "text_total": len(entries),
        "entries": entries,
    }

    OUT.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Wrote {OUT.relative_to(PROJECT)}  ({OUT.stat().st_size/1024:.1f} KB, {len(entries)} entries)")


if __name__ == "__main__":
    main()
