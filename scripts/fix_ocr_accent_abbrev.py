"""Fix text_entries titles where the OCR rendered an acute accent on
the abbreviation vowel — ``(só)`` and ``(sé)`` are not Madoz forms;
they come from the OCR misreading the period after ``so.``/``se.`` as
an accent.

Audit 2026-05-17 (follow-up to fix_ocr_spacing_titles.py). Four FONT
rows in this category, all linked to the same curated diccionariomadoz.com
entry that confirms the unaccented form.

A fifth candidate (MATAGROSA (sé)) was deliberately left out: the
facsimile prints (sé) verbatim, and our policy is to record what is in
the source rather than normalise it.

Same shape as the previous fix scripts: dry-run by default, ``--apply``
writes both the DB and the source JSON. Idempotent.

  python scripts/fix_ocr_accent_abbrev.py            # dry run
  python scripts/fix_ocr_accent_abbrev.py --apply
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
    # Four FONT predios on the same leaf, all linked to curated FONT (SO).
    # The chocr renders '(só)' uniformly — same OCR misread for all four.
    (8337, "FONT (so) [Andraix]"),
    (8339, "FONT (so) [Petra]"),
    (8338, "FONT (so) [San Juan]"),
    (8336, "FONT (so) [Valldemosa]"),
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
