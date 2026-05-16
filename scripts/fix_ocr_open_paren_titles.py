"""Fix text_entries titles where the opening ``(`` of a Mallorquí
suffix was preserved but the rest got OCR-mangled or truncated
(``PAU (so``, ``TORRETA (lan``, ``SALA(so``, ``SERRA (i.\\``, ...).

Audit 2026-05-16 (follow-up to fix_ocr_close_paren_titles.py). Three
batches by how the disambiguation between ``(so)`` / ``(son)`` is
resolved:

1. Linked to a curated diccionariomadoz.com entry — grounded.
2. Unlinked but using a single-form abbreviation (``(la)`` / ``(can)``
   are unambiguous; no ``(lan)`` or ``(cans)`` variant exists).
3. Unlinked AND ``(so)``/``(son)`` ambiguous — resolved by inspecting
   the IA facsimile via the per-row link in the explorer.

Same shape as the previous fix scripts: dry-run by default, ``--apply``
writes both the DB and the source JSON. Idempotent.

  python scripts/fix_ocr_open_paren_titles.py            # dry run
  python scripts/fix_ocr_open_paren_titles.py --apply
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
    (8857, "SALA (so)"),     # was 'SALA(so'    — also fixes the missing space; chocr sibling 'SALA (so)' on same leaf confirms
    (8917, "SERRA (can)"),   # was 'SERRA (i.\\' — curated 103615 = SERRA (CAN). Distinct from 'SERRA (so)' and 'SERRA (son)' on same leaf
    (8985, "TORRETA (la)"),  # was 'TORRETA (lan' — curated 86806 = TORRETA (LA). Trailing 'n' is OCR mangle for ')'
    # Batch 2 — unambiguous single-form abbreviation.
    (8797, "REAL (la)"),     # was 'REAL (la'
    (8838, "ROMÍ (can)"),    # was 'ROMÍ (can'
    (8937, "SIONE (can)"),   # was 'SIONE (can i' — trailing 'i' is OCR mangle for ')'
    (8962, "TALAYOLA (la)"), # was 'TALAYOLA (la'
    # Batch 3 — facsimile-verified by the user.
    (8627, "NEGRE (so)"),    # was 'NEGRE (so'
    (8664, "PAU (so)"),      # was 'PAU (so'
    (8705, "PONT (so)"),     # was 'PONT (so^' — '^' is OCR mangle for ')'
    (8765, "RADÓ (so)"),     # was 'RADÓ (só'  — 'só' is OCR rendering of 'so)'
    (8787, "RAMIS (son)"),   # was 'IUMIS (Son' — facsimile shows the toponym is RAMIS, not IUMIS (R→I, M→U OCR confusion in addition to the truncated close-paren)
    (8885, "PIRIS (so)"),    # was 'PIRIS (so'
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
        print(f"  id={tid:5}  {ot!r:<24} -> {nt!r}")

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
