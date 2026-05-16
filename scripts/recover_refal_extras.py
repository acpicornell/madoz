"""Recover the missing REFAL homonyms on leaf 13/399.

Madoz prints ~9 distinct REFAL articles on this leaf, each for a
different Mallorcan village. Our Claude-text pass only captured the
two collapsed ones (Manacor with "tres distintos", Artá with "dos
predios de igual nombre"). This script adds the 5 missing single-
predio REFAL entries (Bañalbufar, Lluchmayor, Santa Maria del Camí,
Palma, Porreras), and fixes the existing Artá entry whose muni was
left NULL by the LLM.

Idempotent.

  python scripts/recover_refal_extras.py            # dry run
  python scripts/recover_refal_extras.py --apply
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
SOURCE = PROJECT / "data" / "text" / "page_13_399.json"

EXTRAS = [
    {
        "title": "REFAL", "place_type": "predio", "island": "Mallorca",
        "judicial_district": "Palma", "municipality": "Bañalbufar",
        "description": (
            "predio en la isla de Mallorca, prov. de Baleares, part. jud. "
            "de Palma, térm. y jurisd. de la v. de Bañalbufar."
        ),
    },
    {
        "title": "REFAL", "place_type": "predio", "island": "Mallorca",
        "judicial_district": "Palma", "municipality": "Lluchmayor",
        "description": (
            "predio en la isla de Mallorca, prov. de Baleares, part. jud. "
            "de Palma, térm. jurisd. de la v. de Lluchmayor."
        ),
    },
    {
        "title": "REFAL", "place_type": "predio", "island": "Mallorca",
        "judicial_district": "Palma", "municipality": "Santa Maria del Camí",
        "description": (
            "predio en la isla de Mallorca, prov., aud. terr., c. g. de "
            "Baleares, part. jud. de Palma, térm. y jurisd. de la v. de "
            "Sta. Maria."
        ),
    },
    {
        "title": "REFAL", "place_type": "predio", "island": "Mallorca",
        "judicial_district": "Palma", "municipality": "Palma",
        "description": (
            "predio en la isla de Mallorca, prov., aud. terr., c. g. de "
            "Baleares, part. jud., térm. y jurisd. de la c. de Palma."
        ),
    },
    {
        "title": "REFAL", "place_type": "predio", "island": "Mallorca",
        "judicial_district": "Manacor", "municipality": "Porreras",
        "description": (
            "predio en la isla de Mallorca, prov. de Baleares, part. jud. "
            "de Manacor, térm. y jurisd. de la v. de Porreras."
        ),
    },
]
RECOVERY_NOTE = (
    "Recovered manually 2026-05-16: leaf 13/399 has ~9 distinct REFAL "
    "articles for different villages; the LLM only captured the two "
    "collapsed ones (Manacor and Artá). This is one of the missing "
    "single-predio articles."
)


def main() -> None:
    apply = "--apply" in sys.argv
    con = duckdb.connect(str(DB), read_only=not apply)

    # Step 1: fix the existing REFAL (Artá) row that has muni=NULL.
    arta_fix_needed = False
    row = con.execute(
        "SELECT id, municipality FROM text_entries "
        "WHERE id=8806 AND title='REFAL'"
    ).fetchone()
    if row and row[1] != "Artá":
        arta_fix_needed = True
        print(f"  Will set id=8806 municipality NULL → 'Artá'")

    # Step 2: which new entries are missing?
    pending = []
    for entry in EXTRAS:
        existing = con.execute(
            "SELECT id FROM text_entries "
            "WHERE vol='13' AND leaf=399 AND title='REFAL' AND municipality=?",
            [entry["municipality"]],
        ).fetchone()
        if existing:
            print(f"  [skip] REFAL ({entry['municipality']}) already at id={existing[0]}")
        else:
            pending.append(entry)

    print(f"\n  {len(pending)} new REFAL entries to insert.")
    if not pending and not arta_fix_needed:
        return

    if not apply:
        for e in pending:
            print(f"  + REFAL ({e['municipality']}): {e['description'][:80]}…")
        print("\nDRY RUN — pass --apply to commit.")
        return

    # 1. Patch source JSON
    if SOURCE.exists():
        data = json.loads(SOURCE.read_text(encoding="utf-8"))
    else:
        # Source file may have been on a different leaf — create stub.
        data = {"vol": "13", "leaf": 399, "page_printed": "395",
                "window": 2, "model": "claude-opus-4-7-recovered",
                "entries": []}

    # Fix municipality on existing REFAL Artá if it's in this file
    for e in data.get("entries", []):
        if e.get("title") == "REFAL" and e.get("description", "").lower().find("arta") > 0 \
                and not e.get("municipality"):
            e["municipality"] = "Artá"

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

    # 2. Update Artá row
    if arta_fix_needed:
        con.execute(
            "UPDATE text_entries SET municipality='Artá' WHERE id=8806"
        )
        print("  ✓ id=8806 municipality → 'Artá'")

    # 3. Insert new rows
    for entry in pending:
        con.execute(
            """INSERT INTO text_entries
               (vol, leaf, page_printed, title, place_type, island,
                judicial_district, municipality, description, stats,
                cross_references, confidence, window_size, model,
                source_file, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?::JSON, ?, ?, ?, ?, ?, ?)""",
            [
                "13", 399, "395",
                entry["title"], entry["place_type"], entry["island"],
                entry["judicial_district"], entry["municipality"],
                entry["description"], "{}",
                [], "high",
                2, "claude-opus-4-7-recovered",
                "data/text/page_13_399.json", RECOVERY_NOTE,
            ],
        )
        print(f"  ✓ inserted REFAL ({entry['municipality']})")


if __name__ == "__main__":
    main()
