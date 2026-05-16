"""Fix text_entries titles where the closing ``)`` of the ``(so)`` /
``(son)`` Mallorquí suffix got OCR-mangled into ``j`` / ``i`` / ``1`` /
``l`` / ``N`` (e.g. ``OLIVER (soj`` → ``OLIVER (so)``).

Audit 2026-05-16 (follow-up to fix_ocr_digit_titles.py). Two batches:

1. Five rows linked to a curated diccionariomadoz.com entry — the
   disambiguation between ``(so)`` and ``(son)`` is grounded against
   the curated title.
2. Four rows with no curated link (PUSA, RAMON, SARD, SASTRE) —
   resolved by manually inspecting the IA facsimile via the per-row
   IA link in the web explorer.

Same shape as ``fix_ocr_digit_titles.py``: dry-run by default,
``--apply`` writes both the DB and the source JSON. Idempotent.

  python scripts/fix_ocr_close_paren_titles.py            # dry run
  python scripts/fix_ocr_close_paren_titles.py --apply
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
    # Batch 1 — curated diccionariomadoz.com link gives the (so) reading.
    (8623, "NADAL (so)"),    # was 'NADAL (so1)' — Felanitx predio; curated 116537 = NADAL (SO)
    (8624, "NADAL (so)"),    # was 'NADAL (sol'  — Andraitx predio; curated 116539 = NADAL (SO)
    (8641, "OLIVER (so)"),   # was 'OLIVER (soj' — Campos predio; curated 120236 = OLIVER (SO)
    (8869, "SALOM (so)"),    # was 'SALOM (soi'  — Campos predio; curated 39529  = SALOM (so)
    (9045, "VERI (so)"),     # was 'VERI (soN'   — Valldemosa predio; curated 93252 = VERI (SO)
    # Batch 2 — no curated link; readings confirmed by inspecting the IA
    # facsimile via the per-row IA link in the explorer.
    (8756, "PUSA (son)"),    # was 'PUSA (soNj'   — facsimile reads (son)
    (8790, "RAMON (son)"),   # was 'RAMON (so¡'   — facsimile reads (son)
    (8899, "SARD (so)"),     # was 'SARD (soj'    — facsimile reads (so)
    (8904, "SASTRE (so)"),   # was 'SASTRE (so¡v)' — facsimile reads (so)
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
