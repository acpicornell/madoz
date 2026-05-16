"""Deduplicate text_entries rows where a mega-municipality article was
extracted twice from overlapping multi-leaf windows.

Audit 2026-05-17. Five overlapping pairs found via:
    SELECT title, place_type, COUNT(*) FROM text_entries
    WHERE place_type IN ('villa','ciudad','lugar','aldea')
    GROUP BY title, place_type HAVING COUNT(*) > 1

Per-row analysis (description comparison + chocr leaf layout):

- 8572 MANACOR (villa, leaf 172): duplicate of 8574 (leaf 174 — the
  canonical leaf where the article starts in Madoz). Delete.
- 8573 MANCOR (lugar, leaf 172): duplicate of 8575 (leaf 175). Delete.
- 8473 INCA   (villa, leaf 429): duplicate of 8474 (leaf 431, slightly
  longer extraction). Delete.
- 8470 IBIZA  (ciudad, leaf 382): the row's note hoped to contain "the
  beginning of IBIZA partido judicial article" but the actual 1616-char
  description is just a shorter alternative extraction of the same
  IBIZA ciudad article in 8468 (leaf 379). Delete.
- 8594 MAHON  (ciudad, leaf 30): NOT a delete — the 4878-char body
  actually IS the MAHON partido judicial article (opens with "c., cab.
  del part. jud. de su nombre, en la isla y dióc. de Menorca...") that
  has no other home. Re-title to 'MAHON (part. jud.)' and break the
  link to the ciudad curated row (no curated partit row exists yet).

Same shape as the previous fix scripts: dry-run by default, ``--apply``
writes both the DB and the source JSON. Idempotent (deletions are
no-ops if the row is already gone).

  python scripts/dedup_municipality_articles.py            # dry run
  python scripts/dedup_municipality_articles.py --apply
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

# Rows to delete entirely.
DELETIONS: list[int] = [8572, 8573, 8473, 8470]

# Rows to re-title (and optionally clear the madoz_entry_id link if
# it pointed at a different article).
# (text_entry_id, old_title, new_title, new_madoz_entry_id_or_KEEP_AS_NULL)
#   - When the new title belongs to a different article and there is
#     no curated equivalent, pass None to clear the link.
RETITLES: list[tuple[int, str, str, int | None]] = [
    (8594, "MAHON", "MAHON (part. jud.)", None),  # break link to 6389 = MAHON ciudad
]


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    # Plan deletions
    del_plan = []
    for tid in DELETIONS:
        row = con.execute(
            "SELECT title, vol, leaf, source_file FROM text_entries WHERE id=?",
            [tid],
        ).fetchone()
        if not row:
            print(f"  [skip-del] id={tid} already gone")
            continue
        del_plan.append((tid, *row))

    # Plan re-titles
    ret_plan = []
    for tid, old_t, new_t, new_mid in RETITLES:
        row = con.execute(
            "SELECT title, madoz_entry_id, source_file FROM text_entries WHERE id=?",
            [tid],
        ).fetchone()
        if not row:
            print(f"  [skip-ret] id={tid} not found")
            continue
        cur_t, cur_mid, src = row
        if cur_t == new_t and cur_mid == new_mid:
            print(f"  [skip-ret] id={tid} already done")
            continue
        ret_plan.append((tid, cur_t, new_t, cur_mid, new_mid, src))

    print(f"\n{len(del_plan)} deletions:")
    for tid, t, vol, leaf, src in del_plan:
        print(f"  - id={tid}  {t!r}  vol/leaf {vol}/{leaf}  ({src})")
    print(f"\n{len(ret_plan)} re-titles:")
    for tid, ot, nt, om, nm, src in ret_plan:
        link_tag = "" if om == nm else f" (link {om}→{nm})"
        print(f"  - id={tid}  {ot!r} → {nt!r}{link_tag}")

    if not apply:
        if del_plan or ret_plan:
            print("\nDRY RUN — pass --apply to commit.")
        return

    # Apply deletions: drop from JSON file and DB
    for tid, t, vol, leaf, src in del_plan:
        path = PROJECT / src
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = data.get("entries", [])
            data["entries"] = [e for e in entries if e.get("title") != t]
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        con.execute("DELETE FROM text_entries WHERE id=?", [tid])
        print(f"  ✓ deleted id={tid}")

    # Apply re-titles: patch JSON + DB
    for tid, ot, nt, om, nm, src in ret_plan:
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
        if om != nm:
            con.execute(
                "UPDATE text_entries SET title=?, madoz_entry_id=? WHERE id=?",
                [nt, nm, tid],
            )
        else:
            con.execute(
                "UPDATE text_entries SET title=? WHERE id=?", [nt, tid]
            )
        print(f"  ✓ re-titled id={tid}")

    print(f"\nApplied {len(del_plan)} deletions + {len(ret_plan)} re-titles.")


if __name__ == "__main__":
    main()
