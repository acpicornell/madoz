"""Recover the SALAS (isleta) entry that the LLM extraction missed
on leaf 13/684. The chocr text plainly shows two Balearic SALAS
entries on that page — the isleta off Palma and the predi (Can) in
Pollenza — but our pipeline picked up only the predi.

This is a one-shot recovery (idempotent): if the isleta entry
already exists in text_entries, the script is a no-op.

  python scripts/recover_salas_isleta.py            # dry run
  python scripts/recover_salas_isleta.py --apply
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
SOURCE = PROJECT / "data" / "text" / "page_13_684.json"

NEW_ENTRY = {
    "title": "SALAS",
    "place_type": "isleta",
    "island": "Mallorca",
    "judicial_district": "Palma",
    "municipality": "Palma",
    "description": (
        "isleta en la isla, prov. y tercio marítimo de Mallorca, "
        "térm. de la c. de Palma, perteneciente al distr. de Andraitx, "
        "departamento de Cartagena: está sit. á 4 millas de la isla "
        "de la Caleta, y á igual dist. del cabo de Cala Figuera."
    ),
    "stats": {},
    "cross_references": [],
    "confidence": "high",
    "note": (
        "Recovered manually 2026-05-16: the Claude-text pass on leaf 13/684 "
        "captured only SALAS (Can), missing this second Balearic entry on "
        "the same leaf. Description re-transcribed from chocr."
    ),
}


def main() -> None:
    apply = "--apply" in sys.argv
    con = duckdb.connect(str(DB), read_only=not apply)

    # Already present?
    existing = con.execute(
        "SELECT id FROM text_entries "
        "WHERE vol='13' AND leaf=684 AND title='SALAS'"
    ).fetchone()
    if existing:
        print(f"  [skip] SALAS (isleta) already exists at id={existing[0]} — nothing to do.")
        return

    # Find the madoz_entries.id for "SALAS" (the isleta — content_text
    # cites Mallorca).
    rows = con.execute(
        "SELECT id, substr(content_text,1,80) FROM madoz_entries "
        "WHERE title='SALAS'"
    ).fetchall()
    if not rows:
        print("  WARN: no madoz_entries row with title='SALAS' found.")
        new_mid = None
    else:
        # Prefer the one whose content text mentions Mallorca / Palma.
        new_mid = None
        for mid, snippet in rows:
            if "Mallorca" in (snippet or "") or "Palma" in (snippet or ""):
                new_mid = mid
                break
        if new_mid is None:
            new_mid = rows[0][0]
        print(f"  Will link to madoz_entries.id={new_mid}")

    print(f"\n  Will insert into text_entries: vol=13, leaf=684, title='SALAS'")
    print(f"  description: {NEW_ENTRY['description'][:90]}…")
    if not apply:
        print("\nDRY RUN — pass --apply to commit.")
        return

    # 1. Append to source JSON
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    if not any(e.get("title") == "SALAS" and e.get("place_type") == "isleta"
               for e in data.get("entries", [])):
        data.setdefault("entries", []).append(NEW_ENTRY)
        SOURCE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  ✓ added to {SOURCE.relative_to(PROJECT)}")

    # 2. Insert into DB
    con.execute(
        """INSERT INTO text_entries
           (vol, leaf, page_printed, title, place_type, island,
            judicial_district, municipality, description, stats,
            cross_references, confidence, window_size, model,
            source_file, note, madoz_entry_id)
           VALUES
           (?, ?, ?, ?, ?, ?, ?, ?, ?, ?::JSON, ?, ?, ?, ?, ?, ?, ?)""",
        [
            "13", 684, "680",
            NEW_ENTRY["title"], NEW_ENTRY["place_type"], NEW_ENTRY["island"],
            NEW_ENTRY["judicial_district"], NEW_ENTRY["municipality"],
            NEW_ENTRY["description"], json.dumps(NEW_ENTRY["stats"]),
            NEW_ENTRY["cross_references"], NEW_ENTRY["confidence"],
            2, "claude-opus-4-7-recovered",
            "data/text/page_13_684.json", NEW_ENTRY["note"], new_mid,
        ],
    )
    new_id = con.execute(
        "SELECT id FROM text_entries WHERE vol='13' AND leaf=684 AND title='SALAS'"
    ).fetchone()[0]
    print(f"  ✓ inserted into text_entries with id={new_id}")


if __name__ == "__main__":
    main()
