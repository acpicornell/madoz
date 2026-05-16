"""Normalize place_type values: collapse OCR/format variants into a
single canonical form per concept.

Three families of fixes:
  A. snake_case → spaces  (casa_de_campo → casa de campo, ...)
  B. accents lost in OCR  (alqueria → alquería, ...)
  C. plural / qualifier   (predios → predio, predio antiguo → predio, ...)

Run AFTER load_text.py. Idempotent.

  python scripts/normalize_place_types.py            # dry run
  python scripts/normalize_place_types.py --apply    # commit
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

RENAMES: dict[str, str] = {
    # A. snake_case (LLM JSON-key style leaking into values)
    "casa_de_campo": "casa de campo",
    "ciudad_antigua": "ciudad antigua",
    "pueblo_desaparecido": "pueblo desaparecido",
    "isla_antigua": "isla antigua",
    "monte_y_predio": "monte y predio",
    "prado_comun": "prado común",
    "torre_vigia": "torre de vigía",
    # B. accents lost in OCR
    "alqueria": "alquería",
    "caserio": "caserío",
    "porcion de terreno": "porción de terreno",
    "reunion de casas": "reunión de casas",
    "diocesis": "diócesis",
    "casilla de vigia": "casilla de vigía",
    "denominacion historica": "denominación histórica",
    "poblacion desaparecida": "población desaparecida",
    "capitania general": "capitanía general",
    "caserio parroquia rural": "caserío parroquia rural",
    # C. plural / qualifier collapsed into the canonical singular
    "predios": "predio",
    "predio antiguo": "predio",
    "predios antiguos": "predio",
    "islas": "isla",
    "islotes": "islote",
    "isletas": "isleta",
    # D. synonyms describing the same concept
    "casas": "reunión de casas",
    "casas reunidas": "reunión de casas",
    "huertas": "huerta",
    "terreno de huertos": "huerta",
}


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    planned: list[tuple[str, str, int]] = []
    for src, dst in RENAMES.items():
        n = con.execute(
            "SELECT COUNT(*) FROM text_entries WHERE place_type = ?", [src],
        ).fetchone()[0]
        if n:
            planned.append((src, dst, n))

    total = sum(n for _, _, n in planned)
    print(f"{len(planned)} renames pending ({total} rows):")
    for src, dst, n in planned:
        print(f"  {n:4}  {src!r:<22} → {dst!r}")

    if not apply:
        if planned:
            print("\nDRY RUN — pass --apply to commit.")
        return

    for src, dst, _n in planned:
        # Patch JSON source files first (so a future load_text.py won't
        # re-introduce the variant).
        src_files = con.execute(
            "SELECT DISTINCT source_file FROM text_entries WHERE place_type = ?",
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
                if e.get("place_type") == src:
                    e["place_type"] = dst
                    changed = True
            if changed:
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

        # Then DB.
        con.execute(
            "UPDATE text_entries SET place_type = ? WHERE place_type = ?",
            [dst, src],
        )
        print(f"  renamed {src!r} → {dst!r}")


if __name__ == "__main__":
    main()
