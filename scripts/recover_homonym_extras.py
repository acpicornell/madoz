"""Recover the 2 missing-homonym entries surfaced by audit_homonyms.py:

  * PORTO COLOM (puerto ó cala, Mallorca, Felanitx) — leaf 13/172.
    Madoz prints 2 articles for PORTO COLOM on this leaf: the
    estancia/predio (we have it) and the puerto/cala (we missed).
  * RAFAL NOU (predio, Menorca, Mahón) — leaf 13/362. Two homonyms:
    Mallorca/Maria (we have, id=8777) and Menorca/Mahón (missing).

Both transcribed from the chocr text on their respective leaves.
Idempotent.

  python scripts/recover_homonym_extras.py            # dry run
  python scripts/recover_homonym_extras.py --apply
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

EXTRAS = [
    {
        "vol": "13", "leaf": 172, "page_printed": "168",
        "title": "PORTO COLOM",
        "place_type": "puerto",
        "island": "Mallorca",
        "judicial_district": "Manacor",
        "municipality": "Felanitx",
        "description": (
            "puerto ó cala en la isla de Mallorca, tercio marítimo de "
            "este nombre, distrito de Felanitx, sit. á 2 leg. de esta "
            "v. en su térm. jurisd.; pertenece al departamento de "
            "Cartagena."
        ),
        "note": "Recovered manually 2026-05-16: second PORTO COLOM article "
                "on leaf 13/172 (the puerto/cala, distinct from the predio).",
    },
    {
        "vol": "13", "leaf": 362, "page_printed": "358",
        "title": "RAFAL NOU",
        "place_type": "predio",
        "island": "Menorca",
        "judicial_district": "Mahón",
        "municipality": "Mahón",
        "description": (
            "predio en la isla de Menorca, prov. de Baleares, part. "
            "jud., térm. y jurisd. de la c. de Mahón."
        ),
        "note": "Recovered manually 2026-05-16: second RAFAL NOU article "
                "on leaf 13/362, the Menorca homonym (we already have the "
                "Mallorca one at id=8777).",
    },
]


def main() -> None:
    apply = "--apply" in sys.argv
    con = duckdb.connect(str(DB), read_only=not apply)

    pending = []
    for e in EXTRAS:
        # Already present (same vol/leaf/title/muni/place_type)?
        # place_type is part of the key because we may have e.g. a
        # PORTO COLOM (predio) and a PORTO COLOM (puerto) on the
        # same leaf in the same village.
        existing = con.execute(
            """SELECT id FROM text_entries
               WHERE vol=? AND leaf=? AND title=?
                 AND municipality=? AND place_type=?""",
            [e["vol"], e["leaf"], e["title"], e["municipality"], e["place_type"]],
        ).fetchone()
        if existing:
            print(f"  [skip] {e['title']} ({e['place_type']}, {e['municipality']}) already at id={existing[0]}")
        else:
            pending.append(e)

    print(f"\n{len(pending)} new homonym entries to insert.")
    if not pending:
        return
    if not apply:
        for e in pending:
            print(f"  + {e['title']} ({e['island']}/{e['municipality']}): "
                  f"{e['description'][:80]}…")
        print("\nDRY RUN — pass --apply to commit.")
        return

    for e in pending:
        # Patch source JSON
        src_rel = f"data/text/page_{e['vol']}_{e['leaf']}.json"
        src = PROJECT / src_rel
        if src.exists():
            data = json.loads(src.read_text(encoding="utf-8"))
            keys = {(x.get("title"), x.get("municipality"), x.get("place_type"))
                    for x in data.get("entries", [])}
            if (e["title"], e["municipality"], e["place_type"]) not in keys:
                new = {
                    "title": e["title"], "place_type": e["place_type"],
                    "island": e["island"],
                    "judicial_district": e["judicial_district"],
                    "municipality": e["municipality"],
                    "description": e["description"],
                    "stats": {}, "cross_references": [],
                    "confidence": "high", "note": e["note"],
                }
                data.setdefault("entries", []).append(new)
                src.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

        # Insert into DB
        con.execute(
            """INSERT INTO text_entries
               (vol, leaf, page_printed, title, place_type, island,
                judicial_district, municipality, description, stats,
                cross_references, confidence, window_size, model,
                source_file, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?::JSON, ?, ?, ?, ?, ?, ?)""",
            [
                e["vol"], e["leaf"], e["page_printed"],
                e["title"], e["place_type"], e["island"],
                e["judicial_district"], e["municipality"],
                e["description"], "{}",
                [], "high",
                2, "claude-opus-4-7-recovered",
                src_rel, e["note"],
            ],
        )
        print(f"  ✓ inserted {e['title']} ({e['island']}/{e['municipality']})")


if __name__ == "__main__":
    main()
