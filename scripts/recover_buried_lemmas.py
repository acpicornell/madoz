"""Recover Balearic articles surfaced by inner_lemma_audit.py.

These are articles whose lemma was buried mid-paragraph in the chocr
and therefore missed by both the main paragraph-anchored indexer
(``index_volume.py``) and the per-leaf Claude extraction
(``extract_text.py``), which only emits entries with a clear
paragraph break before them.

Each entry below was hand-transcribed from the chocr window
(``data/text/_chocr/page_<vol>_<leaf>.txt``) — short enough that
re-asking Claude for a single buried lemma costs more in
plumbing than it saves over a manual paste. The transcription
cleans obvious OCR noise (broken spaces, ``v``→``y``, particle
re-spacing) but preserves Madoz's own spellings verbatim
(``Santagny`` stays as printed in the IA facsimile).

Pattern follows ``recover_homonym_extras.py``: idempotent, dry-run by
default, ``--apply`` writes both the per-leaf JSON and the DB row.

  python scripts/recover_buried_lemmas.py            # dry run
  python scripts/recover_buried_lemmas.py --apply
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
        # Inner-lemma audit 2026-05-22, Tom 13 leaf 229.
        # Buried after the tail of PROENTE (Orense, peninsular).
        # Curated mirror id=32599 (PROENS (so), Mallorca, predio, Manacor/Santagnv).
        "vol": "13", "leaf": 229, "page_printed": "225",
        "title": "PROENS (so)",
        "place_type": "predio",
        "island": "Mallorca",
        "judicial_district": "Manacor",
        "municipality": "Santagny",
        "description": (
            "Predio en la isla de Mallorca, prov. de Baleares, part. jud. "
            "de Manacor, térm. y jurisd. de la v. de Santagny."
        ),
        "note": (
            "Recovered 2026-05-22 by inner_lemma_audit.py: buried after the "
            "tail of PROENTE (Orense) in chocr leaf 13/229."
        ),
    },
    {
        # Inner-lemma audit 2026-05-22, Tom 15 leaf 185.
        # Buried after a chained 'V. Toya' cross-reference (TUGIA;
        # TLCIENSIS SALTUS). Curated mirror id=88251 (TUGORES (SO),
        # Mallorca, predio).
        "vol": "15", "leaf": 185, "page_printed": "181",
        "title": "TUGORES (so)",
        "place_type": "predio",
        "island": "Mallorca",
        "judicial_district": "Palma",
        "municipality": "Palma",
        "description": (
            "Predio en la isla de Mallorca, prov., aud. terr. y c. g. de "
            "Baleares, part. jud., térm. y jurisd. de la c. de Palma."
        ),
        "note": (
            "Recovered 2026-05-22 by inner_lemma_audit.py: buried after "
            "the 'V. Toya' cross-references TUGIA and TLCIENSIS SALTUS in "
            "chocr leaf 15/185."
        ),
    },
]


def main() -> None:
    apply = "--apply" in sys.argv
    con = duckdb.connect(str(DB), read_only=not apply)

    pending = []
    for e in EXTRAS:
        existing = con.execute(
            """SELECT id FROM text_entries
               WHERE vol=? AND leaf=? AND title=?""",
            [e["vol"], e["leaf"], e["title"]],
        ).fetchone()
        if existing:
            print(f"  [skip] {e['title']} already at id={existing[0]}")
        else:
            pending.append(e)

    print(f"\n{len(pending)} buried-lemma entries to insert.")
    if not pending:
        return
    if not apply:
        for e in pending:
            print(f"  + {e['title']} ({e['island']}/{e['municipality']}): "
                  f"{e['description'][:80]}…")
        print("\nDRY RUN — pass --apply to commit.")
        return

    for e in pending:
        src_rel = f"data/text/page_{e['vol']}_{e['leaf']}.json"
        src = PROJECT / src_rel
        if src.exists():
            data = json.loads(src.read_text(encoding="utf-8"))
            keys = {x.get("title") for x in data.get("entries", [])}
            if e["title"] not in keys:
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
                2, "manual-buried-lemma-recovery",
                src_rel, e["note"],
            ],
        )
        print(f"  ✓ inserted {e['title']} ({e['island']}/{e['municipality']})")


if __name__ == "__main__":
    main()
