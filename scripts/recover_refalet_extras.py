"""Recover the 3 missing REFALET entries on leaf 13/400.

Madoz on this leaf prints FOUR distinct REFALET articles (homonym
predios in Manacor, Algaida, Lluchmayor and Artá). Our Claude-text
pass only captured the first one; the chocr regex saw all but the
LLM stopped at one. Adds the missing three; idempotent.

  python scripts/recover_refalet_extras.py            # dry run
  python scripts/recover_refalet_extras.py --apply
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
SOURCE = PROJECT / "data" / "text" / "page_13_400.json"

EXTRAS = [
    {
        "title": "REFALET",
        "place_type": "predio",
        "island": "Mallorca",
        "judicial_district": "Palma",
        "municipality": "Algaida",
        "description": (
            "predio en la isla de Mallorca, prov., aud. terr., c. g. de "
            "Baleares, part. jud. de Palma, térm. y jurisd. de la v. "
            "de Algaida."
        ),
    },
    {
        "title": "REFALET",
        "place_type": "predio",
        "island": "Mallorca",
        "judicial_district": "Palma",
        "municipality": "Lluchmayor",
        "description": (
            "predio en la isla de Mallorca, prov. de Baleares, part. "
            "jud. de Palma, térm. y jurisd. de la v. de Lluchmayor."
        ),
    },
    {
        "title": "REFALET",
        "place_type": "predio",
        "island": "Mallorca",
        "judicial_district": "Manacor",
        "municipality": "Artá",
        "description": (
            "predio en la isla de Mallorca, prov., aud. terr., c. g. "
            "de Baleares, part. jud. de Manacor, térm. jurisd. de la "
            "v. de Artá."
        ),
    },
]
RECOVERY_NOTE = (
    "Recovered manually 2026-05-16: leaf 13/400 has 4 distinct REFALET "
    "predios (Manacor, Algaida, Lluchmayor, Artá). The Claude-text pass "
    "captured only the Manacor one; this is the missing N-th."
)


def main() -> None:
    apply = "--apply" in sys.argv
    con = duckdb.connect(str(DB), read_only=not apply)

    # Skip any already present (same vol/leaf/title/municipality).
    pending = []
    for entry in EXTRAS:
        existing = con.execute(
            "SELECT id FROM text_entries "
            "WHERE vol='13' AND leaf=400 AND title=? AND municipality=?",
            [entry["title"], entry["municipality"]],
        ).fetchone()
        if existing:
            print(f"  [skip] REFALET ({entry['municipality']}) "
                  f"already exists at id={existing[0]}")
        else:
            pending.append(entry)

    print(f"\n{len(pending)} new REFALET entries to insert.")
    if not pending:
        return

    if not apply:
        for e in pending:
            print(f"  + REFALET ({e['municipality']}): {e['description'][:80]}…")
        print("\nDRY RUN — pass --apply to commit.")
        return

    # 1. Append to source JSON
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    existing_keys = {
        (e.get("title"), e.get("municipality"))
        for e in data.get("entries", [])
    }
    for entry in pending:
        if (entry["title"], entry["municipality"]) in existing_keys:
            continue
        e = {**entry, "stats": {}, "cross_references": [],
             "confidence": "high", "note": RECOVERY_NOTE}
        data.setdefault("entries", []).append(e)
    SOURCE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  ✓ updated {SOURCE.relative_to(PROJECT)}")

    # 2. Insert into DB. No REFALET in madoz_entries → madoz_entry_id NULL.
    for entry in pending:
        con.execute(
            """INSERT INTO text_entries
               (vol, leaf, page_printed, title, place_type, island,
                judicial_district, municipality, description, stats,
                cross_references, confidence, window_size, model,
                source_file, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?::JSON, ?, ?, ?, ?, ?, ?)""",
            [
                "13", 400, "396",
                entry["title"], entry["place_type"], entry["island"],
                entry["judicial_district"], entry["municipality"],
                entry["description"], "{}",
                [], "high",
                2, "claude-opus-4-7-recovered",
                "data/text/page_13_400.json", RECOVERY_NOTE,
            ],
        )
        print(f"  ✓ inserted REFALET ({entry['municipality']})")


if __name__ == "__main__":
    main()
