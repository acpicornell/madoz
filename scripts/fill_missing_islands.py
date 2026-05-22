"""Fill NULL island fields when the description explicitly cites a
Balearic island ("isla de Mallorca", "isla de Menorca", etc.).

The LLM extractor sometimes omits the island field even when the text
plainly states it. This script is a deterministic completer over what
already lives in the description.

Run AFTER load_text.py. Idempotent.

  python scripts/fill_missing_islands.py            # dry run
  python scripts/fill_missing_islands.py --apply    # commit
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

# Canonical regex: clean «isla de X» as Madoz typesets it.
ISLAND_RE = re.compile(
    r"isla\s+(?:de\s+|del\s+|de\s*l\s*|y\s+di[oó]c\.\s+de\s+)?"
    r"(mallorca|menorca|ibiza|iviza|formentera|cabrera)",
    re.IGNORECASE,
)

# OCR-tolerant rescue regex: 'isla' may be rendered i¿la / ¡sla / irla /
# i¿la / ele, and 'Mallorca' as Mal'orca (with a curly quote) or
# Mal’orca. Variants below were all observed in this corpus. The
# island-name half mirrors the patterns from scripts/index_volume.py's
# Balearic filter (Mallor-mangled / Menor-mangled / Iviza-mangled etc.).
ISLAND_RE_OCR = re.compile(
    r"(?:isla|i[¿¡]?sla|irla|ele|laisla|i\s*sla|i\.la)"
    r".{0,30}?"
    r"(?:(mallorca|mal['’]orca|mall[o0]rca|mall\.|"
    r"menorca|men[o0]rca|"
    r"ibiza|iviza|eivissa|"
    r"formentera|cabrera))",
    re.IGNORECASE,
)

# Additional rescue: "tercio marítimo de X" / "prov. marítima de X" / "
# departamento de Cartagena, ... distrito de X" — strong island signals
# that the OCR-rescue regex above misses when 'isla' is heavily damaged.
ISLAND_RE_MARITIME = re.compile(
    r"(?:tercio\s+mar[ií]t(?:imo)?\.?|prov\.?\s+mar[ií]t(?:imo|ima)?\.?"
    r"|aud\.?\s+terr\.?(?:itorial)?)\s+(?:de\s+|y\s+)?"
    r"(mallorca|menorca|ibiza|iviza|formentera|cabrera)",
    re.IGNORECASE,
)

# Final fallback: the judicial-district field is a strong island signal.
JUD_TO_ISLAND = {
    'palma': 'Mallorca', 'inca': 'Mallorca', 'manacor': 'Mallorca',
    'mahón': 'Menorca', 'mahon': 'Menorca',
    'ciudadela': 'Menorca', 'ciutadella': 'Menorca',
    'ibiza': 'Ibiza', 'iviza': 'Ibiza',
}

# Normalise to the existing island label spellings used in text_entries
# (Mallorca / Menorca / Ibiza / Formentera / Cabrera). Order matters:
# 'Formentera' contains 'men' and 'cabrera' starts with 'cab', so the
# longer / more-specific tests must run first.
def _canon(name: str) -> str:
    n = name.lower()
    if "form" in n:
        return "Formentera"
    if "cabr" in n:
        return "Cabrera"
    if "mall" in n or "mal'" in n or "mal’" in n:
        return "Mallorca"
    if "men" in n:
        return "Menorca"
    if "ibiz" in n or "iviz" in n or "eivissa" in n:
        return "Ibiza"
    return name.capitalize()


# Titles that legitimately span the whole archipelago — never auto-fill
# them, regardless of what the body text mentions.
ARCHIPELAGO_TITLES = {
    "BALEARES",
    "BALEARES (islas de)",
    "BALEARES (provincia de)",
    "GYMNESIAS",
}


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    rows = con.execute(
        "SELECT id, title, description, judicial_district, source_file "
        "FROM text_entries WHERE island IS NULL"
    ).fetchall()

    plan: list[tuple[int, str, str, str]] = []
    for tid, title, desc, jud, src in rows:
        if title in ARCHIPELAGO_TITLES:
            continue
        isl = None
        if desc:
            m = (ISLAND_RE.search(desc)
                 or ISLAND_RE_OCR.search(desc)
                 or ISLAND_RE_MARITIME.search(desc))
            if m:
                isl = _canon(m.group(1))
        if not isl and jud:
            isl = JUD_TO_ISLAND.get(jud.lower())
        if isl:
            plan.append((tid, title, isl, src))

    print(f"{len(plan)} NULL-island entries to fill:")
    for tid, title, isl, _src in plan:
        print(f"  {tid:5} {title[:38]:<38} → {isl}")

    if not apply:
        if plan:
            print("\nDRY RUN — pass --apply to commit.")
        return

    for tid, title, isl, src in plan:
        path = PROJECT / src
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for e in data.get("entries", []):
                if e.get("title") == title and not e.get("island"):
                    e["island"] = isl
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        con.execute("UPDATE text_entries SET island=? WHERE id=?", [isl, tid])
    print(f"\nFilled {len(plan)} rows.")


if __name__ == "__main__":
    main()
