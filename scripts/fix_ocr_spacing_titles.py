"""Fix text_entries titles with spacing anomalies around the
abbreviation parens — no space before ``(`` (``RACÓ(el)`` → ``RACÓ (el)``)
or a stray space inside (``SAN JUAN (s o)`` → ``SAN JUAN (so)``).

Audit 2026-05-16 (follow-up to fix_ocr_replaced_open_paren.py). Two
batches:

1. Linked to a curated diccionariomadoz.com entry.
2. Unlinked but the spacing/abbreviation is unambiguous.

One row (8847 ROTGER(Sax)) carried an extra letter-level OCR error
beyond the spacing: the curated mirror read ``(San)`` but the
facsimile confirms ``Son Rotger`` is the toponym, so the fix uses
``(son)``. Both the chocr ``Sax`` and the curated ``San`` were wrong.

Same shape as the previous fix scripts: dry-run by default, ``--apply``
writes both the DB and the source JSON. Idempotent.

  python scripts/fix_ocr_spacing_titles.py            # dry run
  python scripts/fix_ocr_spacing_titles.py --apply
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

# (text_entry_id, new_title).
FIXES: list[tuple[int, str]] = [
    # Batch 1 — curated link grounds the reading.
    (8794, "RATXÓ (el)"),     # was 'RATXÓ(el)';   curated 35068 = RATXÓ (El)
    (8847, "ROTGER (son)"),   # was 'ROTGER(Sax)'; facsimile confirms 'Son Rotger' — both the chocr 'Sax' and the curated mirror's '(San)' are wrong
    (8954, "SUREDA (can)"),   # was 'SUREDA(can)'; curated 106715 = SUREDA (CAN)
    (9002, "TONI PAU (can)"), # was 'TONI PAU(can)'; curated 85298 = TONI PAU (CAN)
    # Batch 2 — unlinked but unambiguous.
    (8764, "RACÓ (el)"),      # was 'RACÓ(el)'
    (8872, "SAN JUAN (so)"),  # was 'SAN JUAN (s o)' — stray space inside the abbreviation
]


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    plan = []
    for tid, new_title in FIXES:
        row = con.execute(
            "SELECT title, source_file FROM text_entries WHERE id=?",
            [tid],
        ).fetchone()
        if not row:
            print(f"  [skip] id={tid} not found")
            continue
        old_title, src = row
        if old_title == new_title:
            print(f"  [skip] id={tid} already fixed")
            continue
        plan.append((tid, old_title, new_title, src))

    print(f"{len(plan)} fixes pending:")
    for tid, ot, nt, src in plan:
        print(f"  id={tid:5}  {ot!r:<22} -> {nt!r}")

    if not apply:
        if plan:
            print("\nDRY RUN — pass --apply to commit.")
        return

    for tid, ot, nt, src in plan:
        path = PROJECT / src
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for e in data.get("entries", []):
                if e.get("title") == ot:
                    e["title"] = nt
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        con.execute("UPDATE text_entries SET title=? WHERE id=?", [nt, tid])
        print(f"  ✓ id={tid}")

    print(f"\nApplied {len(plan)} fixes.")


if __name__ == "__main__":
    main()
