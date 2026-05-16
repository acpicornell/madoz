"""Apply curated title corrections + link fixes that the audit
report (scripts/audit_similarity.py) surfaced.

Each entry in FIXES carries (text_entry_id, new_title) and optionally
a new madoz_entry_id when the previous fuzzy link was wrong. The
script touches both the DB row and the source JSON so a future
load_text.py keeps the correction.

Run after auditing. Idempotent — re-applying a fix is a no-op.

  python scripts/fix_title_mismatches.py            # dry run
  python scripts/fix_title_mismatches.py --apply
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

# (text_entry_id, new_title, new_madoz_entry_id_or_None_to_keep)
FIXES: list[tuple[int, str, int | None]] = [
    # Pure title corrections — link is already right.
    (8915, "SERVERA (so)",                 None),
    (8779, "RAFAL RUBÍ VELL",              None),
    (8886, "SANTANYI",                     None),
    (8699, "POLLENTIA",                    None),
    (7886, "ALCARIA-BLANCA (dos predios)", None),
    # Title + relink: the fuzzy linker matched a homonym, not the real
    # madoz entry. New mid points at the correct row.
    (7885, "ALCARIA-BLANCA",                4309),   # was 4312 (the predios)
    (8860, "SALAS (can)",                   39167),  # was 39163 (the isleta)
    (8079, "CAMPANER (son)",                21818),  # was 21824 (CAMPANET vila)
    # Second wave (2026-05-16, audit follow-up): missing hyphens and
    # OCR mangles in the title that survived the first cleanup.
    (8232, "CUEVA-LARGA",                   None),   # add hyphen
    (8008, "BINI-BECA",                     None),   # add hyphen
    (7991, "BENISALEM",                     None),   # Madoz's spelling (his 'e', not modern 'i')
    (8021, "BINISAFULLA ó BINI-SAFAYA",     None),   # hyphen in second alt
    # LLUGALGARI is OCR for LLUCALCARI (hamlet in Deyá, the aldea
    # described in the chocr text) — NOT LLUCALARI (SAN ANTONIO DE)
    # which is the nearby feligresía. Re-link accordingly.
    (8545, "LLUCALCARI",                    116020),  # was 116018 (LLUCALARI SAN ANTONIO DE)
]


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    plan = []
    for tid, new_title, new_mid in FIXES:
        row = con.execute(
            "SELECT title, madoz_entry_id, source_file FROM text_entries WHERE id=?",
            [tid],
        ).fetchone()
        if not row:
            print(f"  [skip] id={tid} not found")
            continue
        old_title, old_mid, src = row
        title_change = old_title != new_title
        link_change = new_mid is not None and old_mid != new_mid
        if not title_change and not link_change:
            print(f"  [skip] id={tid} already fixed")
            continue
        plan.append((tid, old_title, new_title, old_mid, new_mid, src))

    print(f"{len(plan)} fixes pending:")
    for tid, ot, nt, om, nm, src in plan:
        tag = []
        if ot != nt:
            tag.append("title")
        if nm is not None and om != nm:
            tag.append(f"link {om}→{nm}")
        print(f"  id={tid:5}  {ot!r:<32} → {nt!r:<30}  ({', '.join(tag)})")

    if not apply:
        if plan:
            print("\nDRY RUN — pass --apply to commit.")
        return

    for tid, ot, nt, om, nm, src in plan:
        # 1. Patch the source JSON
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
        # 2. Patch the DB
        if nm is not None and om != nm:
            con.execute(
                "UPDATE text_entries SET title=?, madoz_entry_id=? WHERE id=?",
                [nt, nm, tid],
            )
        else:
            con.execute(
                "UPDATE text_entries SET title=? WHERE id=?",
                [nt, tid],
            )
        print(f"  ✓ id={tid}")

    print(f"\nApplied {len(plan)} fixes.")


if __name__ == "__main__":
    main()
