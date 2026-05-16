"""Normalize municipality values: collapse OCR mangles, accent variants,
abbreviations and truncations into a single canonical form per place.

Run AFTER load_text.py. Idempotent.

Canonical forms follow Madoz's Castilian spelling (the source language)
rather than modern Catalan — Pollenza not Pollença, Soller not Sóller,
Santañí not Santanyí, etc. — to keep continuity with the OCR'd text.

  python scripts/normalize_municipalities.py            # dry run
  python scripts/normalize_municipalities.py --apply    # commit
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

# Renames map: variant → canonical. ``None`` means delete the field
# (set NULL in DB / drop from JSON).
RENAMES: dict[str, str | None] = {
    # Pollença
    "Pollensa": "Pollenza",
    "Polienza": "Pollenza",
    "Pullenza": "Pollenza",
    # Santa Margarita
    "Sta. Margarita": "Santa Margarita",
    "Sta": "Santa Margarita",  # all 5 truncated rows describe "Sta. Margarita"
    # Mahón
    "Mahon": "Mahón",
    # Sansellas / Sencelles
    "Sancellas": "Sansellas",
    # Maria (de la Salud)
    "María": "Maria",
    "María de la Salud": "Maria",
    # Andraitx
    "Andraix": "Andraitx",
    # Artá
    "Arta": "Artá",
    # Santanyí / Santañí
    "Santanyí": "Santañí",
    "Santagny": "Santañí",
    "Santagni": "Santañí",
    # Esporlas
    "Espolias": "Esporlas",
    # Bañalbufar
    "Rañalbufar": "Bañalbufar",
    # La Puebla (sa Pobla)
    "Puebla": "La Puebla",
    "La": "La Puebla",  # TALAPÍ: OCR concat "LaPuebla" → field captured as "La"
    # Lluchmayor
    "Lluchmavor": "Lluchmayor",
    # Valldemosa
    "Valldomosa": "Valldemosa",
    "Validemora": "Valldemosa",
    "Videmora": "Valldemosa",
    "Valide": "Valldemosa",
    "Vall": "Valldemosa",  # NOGUERAL, Palma jud., desc "v. de Vall-." (truncat OCR)
    # Binisalem
    "Benisalem": "Binisalem",
    # Felanitx
    "Eelanitx": "Felanitx",
    "Felanich": "Felanitx",
    "Felanilx": "Felanitx",
    "Felanitz": "Felanitx",
    # Ferrerías
    "Ferrerias": "Ferrerías",
    # Manacor
    "Menacor": "Manacor",
    "Mauacor": "Manacor",
    "Mannror": "Manacor",
    # Sineu
    "Sincu": "Sineu",
    # Buger
    "Bugcr": "Buger",
    # Campanet
    "Gampanet": "Campanet",
    # Escorca
    "Escoren": "Escorca",
    # Porreras
    "Porrera": "Porreras",
    # Alaró
    "Alaré": "Alaró",
    # San Antonio (Ibiza)
    "San Antonio Abad": "San Antonio",
    # Santa Eulalia (Ibiza)
    "Sta. Eulalia": "Santa Eulalia",
    # Llubí
    "Llubi": "Llubí",
    "Lluví": "Llubí",
    # Santa María del Camí
    "Sta. Maria": "Santa Maria del Camí",
    # San Juan (Mallorca) — distinct from "San Juan Bautista" (Ibiza)
    "San": "San Juan",
    # Calviá
    "Calvia": "Calviá",
    # Mataró: OCR bug — GUITART (son) is actually in Alaró (per user).
    "Mataró": "Alaró",
    # GYMNESIAS province-wide article has empty-string muni.
    "": None,
}


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    planned: list[tuple[str, str | None, int]] = []
    for src, dst in RENAMES.items():
        n = con.execute(
            "SELECT COUNT(*) FROM text_entries WHERE municipality = ?", [src],
        ).fetchone()[0]
        if n:
            planned.append((src, dst, n))

    total = sum(n for _, _, n in planned)
    print(f"{len(planned)} renames pending ({total} rows):")
    for src, dst, n in planned:
        print(f"  {n:4}  {src!r:<24} → {dst!r}")

    if not apply:
        if planned:
            print("\nDRY RUN — pass --apply to commit.")
        return

    for src, dst, _n in planned:
        # Patch JSON source files first.
        src_files = con.execute(
            "SELECT DISTINCT source_file FROM text_entries WHERE municipality = ?",
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
                if e.get("municipality") == src:
                    if dst is None:
                        e.pop("municipality", None)
                    else:
                        e["municipality"] = dst
                    changed = True
            if changed:
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

        if dst is None:
            con.execute(
                "UPDATE text_entries SET municipality = NULL WHERE municipality = ?",
                [src],
            )
        else:
            con.execute(
                "UPDATE text_entries SET municipality = ? WHERE municipality = ?",
                [dst, src],
            )
        print(f"  renamed {src!r} → {dst!r}")


if __name__ == "__main__":
    main()
