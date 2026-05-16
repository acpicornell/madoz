"""Fix text_entries titles where the opening ``(`` of the abbreviation
suffix got replaced by a stray character (``í``, ``f``, ``¡``, ``^``,
``,'``, ``Í``, etc.) or dropped entirely while the closing ``)`` is
still present (e.g. ``RUSINOL fso)`` → ``RUSINOL (so)``, ``PERETÓ so)``
→ ``PERETÓ (so)``).

Audit 2026-05-16 (follow-up to fix_ocr_open_paren_titles.py). Two
batches:

1. Linked to a curated diccionariomadoz.com entry or with a clear
   chocr reading that matches the curated title.
2. Unlinked but the leftover characters unambiguously encode the
   abbreviation (``¡so)``, ``Ícova``, ``íla)`` → ``(so)``, ``(cova``,
   ``(la)``).

One row (8724 'POS YERL) \\So)' in vol 13 leaf 176) is too mangled to
guess and stays out of this batch; needs IA facsimile inspection.

Same shape as the previous fix scripts: dry-run by default, ``--apply``
writes both the DB and the source JSON. Idempotent.

  python scripts/fix_ocr_replaced_open_paren.py            # dry run
  python scripts/fix_ocr_replaced_open_paren.py --apply
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
    # Batch 1 — curated/chocr-grounded.
    (8852, "RUSINOL (so)"),               # was 'RUSINOL fso)';   curated 38604 = RUSINOL (so)
    (8931, "SIMÓ (so)"),                  # was 'SIMÓ íso)';      curated 104362 = SIMÓ (SO)
    (8930, "SIMÓ (son)"),                 # was "SIMÓ ,'son)";    fuzzy-linker incorrectly tied this row to 104362 (= SIMÓ (SO)) but the chocr clearly reads 'son)', and a separate clean 'SIMÓ (son)' row exists on the same leaf — title trusted over the bad link
    (9055, "VILLARET DE LLENAIRE (el)"),  # was '… ^el)';         curated 98931 = VILLARET DE LLENAIRE (EL)
    (8893, "PLANES (ses)"),               # was 'PLANES ^ses)';   curated 29583 says 'PLANESES' (single word) which is not a known Mallorquí toponym and is more likely the curated mirror's own OCR error; chocr suffix '^ses)' clearly encodes '(ses)'
    # Batch 2 — unambiguous from the leftover characters.
    (8685, "PERETÓ (so)"),                # was 'PERETÓ so)';     sibling 'PERET (so)' on same leaf confirms the convention
    (8710, "PORCHS (cova dels)"),         # was 'PORCHSÍcova dels)'; 'Í' is OCR mangle for '('. Description is about a cova (cave)
    (8763, "RACHAL (so)"),                # was 'RACHAL ¡so)';    '¡' is OCR mangle for '('
    (8913, "SEBELLETA (la)"),             # was 'SEBELLETA íla)'; 'í' is OCR mangle for '('
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
        print(f"  id={tid:5}  {ot!r:<28} -> {nt!r}")

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
