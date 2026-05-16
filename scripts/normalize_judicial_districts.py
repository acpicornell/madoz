"""Normalize OCR-mangled / accent-stripped judicial_district values to
their canonical Balearic forms.

Run AFTER load_text.py and purge_non_balearic.py. Idempotent.

  python scripts/normalize_judicial_districts.py            # dry run
  python scripts/normalize_judicial_districts.py --apply    # commit
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

# Canonical mapping: every value not in this set gets reported (so we
# notice new OCR variants creeping in). The empty string maps to None,
# which we translate to SQL NULL.
RENAMES: dict[str, str | None] = {
    "Mahon": "Mahón",
    "Mabon": "Mahón",
    "InCa": "Inca",
    "Manaoor": "Manacor",
    "Manucor": "Manacor",
    "Ciudadelaprov": "Ciudadela",
    "": None,
}

CANONICAL = {"Palma", "Inca", "Manacor", "Mahón", "Ciudadela", "Ibiza"}


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    # Audit: report any value that isn't canonical AND isn't in RENAMES.
    rows = con.execute(
        "SELECT judicial_district, COUNT(*) FROM text_entries "
        "WHERE judicial_district IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    untracked = [
        (v, n) for v, n in rows
        if v not in CANONICAL and v not in RENAMES
    ]
    if untracked:
        print("WARN: untracked judicial_district values (not normalized):")
        for v, n in untracked:
            print(f"  {n:4}  {v!r}")
        print()

    # Plan: count rows per rename target.
    planned: list[tuple[str, str | None, int]] = []
    for src, dst in RENAMES.items():
        n = con.execute(
            "SELECT COUNT(*) FROM text_entries WHERE judicial_district = ?",
            [src],
        ).fetchone()[0]
        if n:
            planned.append((src, dst, n))

    print(f"{len(planned)} renames pending:")
    for src, dst, n in planned:
        print(f"  {n:4}  {src!r:<18} → {dst!r}")

    if not apply:
        if planned:
            print("\nDRY RUN — pass --apply to commit.")
        return

    # Apply DB updates and source-JSON rewrites in lockstep.
    for src, dst, _n in planned:
        # Source files to touch (per affected row).
        src_files = con.execute(
            "SELECT DISTINCT source_file FROM text_entries WHERE judicial_district = ?",
            [src],
        ).fetchall()
        for (rel,) in src_files:
            path = PROJECT / rel
            if not path.exists():
                print(f"  WARN: source file missing: {rel}")
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            changed = False
            for e in data.get("entries", []):
                if e.get("judicial_district") == src:
                    if dst is None:
                        e.pop("judicial_district", None)
                    else:
                        e["judicial_district"] = dst
                    changed = True
            if changed:
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        if dst is None:
            con.execute(
                "UPDATE text_entries SET judicial_district = NULL "
                "WHERE judicial_district = ?", [src],
            )
        else:
            con.execute(
                "UPDATE text_entries SET judicial_district = ? "
                "WHERE judicial_district = ?", [dst, src],
            )
        print(f"  renamed {src!r} → {dst!r}")


if __name__ == "__main__":
    main()
