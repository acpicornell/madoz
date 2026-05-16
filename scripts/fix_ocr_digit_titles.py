"""Fix text_entries titles where OCR substituted a digit for a letter
(e.g. ``P1CORNELL`` → ``PICORNELL``, ``RUTLL0`` → ``RUTLLO``).

Surfaced by a manual audit on 2026-05-16: the LLM extraction copied the
chocr OCR mangle verbatim instead of cleaning it. Each row's correction
was verified against the source JSON description and, when available,
the curated diccionariomadoz.com title for the linked ``madoz_entry_id``.

Same shape as ``fix_title_mismatches.py``: dry-run by default, ``--apply``
writes both the DB and the source JSON so a future ``load_text.py`` keeps
the correction. Idempotent.

  python scripts/fix_ocr_digit_titles.py            # dry run
  python scripts/fix_ocr_digit_titles.py --apply
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

# (text_entry_id, new_title). All ten rows are pure title fixes — the
# existing madoz_entry_id links are correct (where present).
FIXES: list[tuple[int, str]] = [
    (8713, "PICORNELL (so)"),          # 1 → I
    (8809, "REFAL DE LOS PORES"),      # 3 → S (minimal change; PORES may itself be an OCR read of PORCS, but that is a letter-letter issue out of scope here)
    (8815, "REFAL PALLA GROS"),        # 0 → O, 3 → S
    (8854, "RUTLLO"),                  # 0 → O (curated mirror has RUTILLO, but that is an extra inserted letter; minimal-change rendering wins until image-verified)
    (8895, "SANTIANI DE BONNABE"),     # 1 → I
    (8896, "SANTIANI DEN MARTORELL"),  # 1 → I
    (8898, "SARANI (son)"),            # 1 → I
    (8947, "SON PIERAS"),              # SUN → SON, 1 → I (curated mirror agrees on SON PIERAS)
    (8949, "SORT DE SA CAPITANA"),     # \ → A, 1 → I, SE → SA (curated has SE but Mallorquí toponym is "Sort de sa Capitana"; backslash in chocr is the giveaway that the article got mangled too)
    (9069, "VIGUET (so)"),             # 1 → I
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
        print(f"  id={tid:5}  {ot!r:<28} → {nt!r}")

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
