"""Purge non-Balearic false-positive entries from text_entries and their
source JSON files.

The LLM extractor occasionally captures entries that share a leaf (or
even a column) with a legitimate Balearic article but actually describe
a homonym elsewhere in Spain (PETRA in Huesca, PILAR in Málaga, SALAS
in Orense, TORO in Orense, COLL DE RATES in Alicante, etc.). The audit
heuristic: description explicitly cites a non-Balearic province via
"prov. de <Name>".

Idempotent: re-running after a fresh load_text.py / extraction will
re-detect anything new and won't double-delete what's already gone.

  python scripts/purge_non_balearic.py            # dry run
  python scripts/purge_non_balearic.py --apply    # commit
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

_PROV_LIST = (
    r"huesca|malaga|m[áa]laga|castell[óo]n|alicante|valencia|teruel|le[óo]n|"
    r"granada|c[áa]diz|sevilla|burgos|salamanca|toledo|murcia|barcelona|"
    r"gerona|girona|l[ée]rida|lleida|c[óo]rdoba|navarra|zaragoza|tarragona|"
    r"albacete|almer[íi]a|badajoz|c[áa]ceres|ciudad real|cu[ea]?[dn]ca|"  # cuenca + "CueDca" OCR
    r"guadalajara|ja[ée]n|logro[ñn]o|lugo|orense|asturias|oviedo|palencia|"
    r"pontevedra|santander|segovia|soria|valladolid|vizcaya|zamora|huelva|"
    r"[áa]vila|alava|guipuzcoa"
)
# Match "prov." within ~40 chars of a non-Balearic province name.
# Permissive on the fill chars to catch real Madoz forms like:
#   "prov. de Granada"
#   "prov.\"de Granada"        (OCR artifact)
#   "prov. y dióc. de Gerona"
#   "prov., aud. terr., c. g. de Barcelona"
# Won't match the canonical Balearic pattern ("prov. de Baleares")
# because Baleares isn't in _PROV_LIST.
PROVINCES_RE = re.compile(
    r"prov\..{0,40}?\b(?:" + _PROV_LIST + r")\b",
    re.IGNORECASE | re.DOTALL,
)


# Titles of entries that are unambiguously non-Balearic but whose
# description fragment doesn't cite a province explicitly (often the
# OCR window grabbed the *tail* of a peninsular article, so the
# province name lives upstream of what we captured). These were
# audited manually:
#   TRUCHAS    — iron works (León), "ferrerias y molinos harineros"
#   VILLAFRANCA — Zumalacárregui / Carlist War, Navarra
#   VILANOVA   — Pico Sacro, Galicia (Pontevedra)
#   SEGURA     — peninsular village fragment, no Balearic markers
#   PUIG       — El Puig (Valencia): Puzol, Puebla de Farnals, Rafelbuñol
CURATED_TITLES_TO_PURGE = {
    "TRUCHAS",
    "VILLAFRANCA",
    "VILANOVA",
    "SEGURA",
    "PUIG",
    "TAEDO",  # "prov. dOviedo" — apostrophe lost, regex \b can't see boundary
}


def is_non_balearic(desc: str | None, title: str | None = None) -> bool:
    if title in CURATED_TITLES_TO_PURGE:
        return True
    if not desc:
        return False
    return bool(PROVINCES_RE.search(desc))


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    rows = con.execute(
        "SELECT id, title, description, source_file FROM text_entries"
    ).fetchall()
    targets = [(tid, title, src) for tid, title, desc, src in rows
               if is_non_balearic(desc, title)]

    print(f"Found {len(targets)} non-Balearic entries to purge.")
    for tid, title, src in targets:
        print(f"  id={tid:5} {title:<35} ← {src}")

    if not apply:
        if targets:
            print("\nDRY RUN — pass --apply to commit.")
        return

    # 1. Trim each source JSON.
    for tid, title, src in targets:
        path = PROJECT / src
        if not path.exists():
            print(f"  WARN: source file missing: {src}")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        before = len(data.get("entries", []))
        data["entries"] = [
            e for e in data.get("entries", []) if e.get("title") != title
        ]
        after = len(data["entries"])
        if before == after:
            print(f"  WARN: {title!r} not found in {src}")
            continue
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  trimmed {src}: {before} → {after} entries")

    # 2. Delete from text_entries.
    ids = [t[0] for t in targets]
    placeholders = ",".join("?" * len(ids))
    con.execute(
        f"DELETE FROM text_entries WHERE id IN ({placeholders})", ids
    )
    print(f"\nDeleted {len(ids)} rows from text_entries.")


if __name__ == "__main__":
    main()
