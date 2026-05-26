"""Audit text_entries.title against the NGIB gazetteer.

A title that fuzzy-matches an NGIB toponym is strong evidence it's a
real place (Madoz wrote it the way it's still recorded on the
official Govern Balear nomenclàtor). A title that matches *nothing* in
NGIB across multiple normalisations is either:
  (a) a historical possession / feature NGIB no longer catalogues, OR
  (b) an OCR-mangled string that doesn't correspond to any real place.

The audit handles the Castilian (Madoz) ↔ Catalan (NGIB) gap with
several normalisation passes:
  - strip accents, uppercase, alphanum only ('SóLLER' = 'SOLLER')
  - Castilian → Catalan toponym map (Ibiza→Eivissa, Mahón→Maó, …)
  - word-order swap for Madoz 'X (Son)' ↔ NGIB 'Son X'
  - prefix stripping for 'CA / CAS / SON / SO / PI DE / POU / PLA / TOR'
    (also fold 'SAN' ↔ 'SANT', 'SANTA' ↔ 'SANTA')
  - rapidfuzz ratio ≥ 78 on the normalised form

Read-only. Reports counts + the no-match list grouped by place_type.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from pathlib import Path

import duckdb
import pandas as pd
from rapidfuzz import fuzz, process

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
GAZ = PROJECT / "data" / "gazetteer.parquet"


# Castilian → Catalan canonical municipality / well-known toponym map.
# Madoz writes the Castilian form; NGIB uses Catalan.
CAST2CAT = {
    "IBIZA": "EIVISSA",
    "IVIZA": "EIVISSA",
    "MAHON": "MAO",
    "CIUDADELA": "CIUTADELLA",
    "POLLENZA": "POLLENCA",
    "BUNOLA": "BUNYOLA",
    "ALCUDIA": "ALCUDIA",
    "LLUCHMAYOR": "LLUCMAJOR",
    "ARTA": "ARTA",
    "PORRERAS": "PORRERES",
    "MARRATXI": "MARRATXI",
    "MURO": "MURO",
    "FELANITX": "FELANITX",
    "INCA": "INCA",
    "SINEU": "SINEU",
    "MANACOR": "MANACOR",
    "CAMPOS": "CAMPOS",
    "ANDRATX": "ANDRATX",
    "ANDRAIX": "ANDRATX",
    "CALVIA": "CALVIA",
    "ESPORLAS": "ESPORLES",
    "BANALBUFAR": "BANYALBUFAR",
    "VALLDEMOSA": "VALLDEMOSSA",
    "SOLLER": "SOLLER",
    "FORNALUTX": "FORNALUTX",
    "ESCORCA": "ESCORCA",
    "CAMPANET": "CAMPANET",
    "SELVA": "SELVA",
    "LLUBI": "LLUBI",
    "MARIA": "MARIA",   # María de la Salut
    "PETRA": "PETRA",
    "SANTAGNY": "SANTANYI",
    "SANTANYI": "SANTANYI",
    "DEYA": "DEIA",
    "DEVA": "DEIA",
    "ALAYOR": "ALAIOR",
    "ALARO": "ALARO",
    "MERCADAL": "MERCADAL",
    "FERRERIAS": "FERRERIES",
    "VILLAFRANCA": "VILAFRANCA",
    "MONTUIRI": "MONTUIRI",
    "ARIANY": "ARIANY",
    # Saint forms
    "SAN": "SANT",
    "SANTA": "SANTA",
}


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def compress(s: str) -> str:
    if not s:
        return ""
    s = _strip_accents(s).upper()
    return re.sub(r"[^A-Z]", "", s)


def normalise_madoz_title(title: str) -> list[str]:
    """Return one or more candidate forms to try against NGIB.

    Madoz title shapes:
      - 'PALMA'
      - 'SAN VICENTE'
      - 'RAMIS (Son)'          → also try 'SON RAMIS'
      - 'BLANCO (cabo, Mallorca)' → also try 'CABO BLANCO'
      - 'ALQUERIA DE LA CONDESA'
      - 'CA SEROL' / 'CAS BRAU' (Mallorquí 'Cas X' contraction)
    """
    if not title:
        return []
    # Strip accents, uppercase
    base = _strip_accents(title).upper()
    # Apply Castilian→Catalan word map (token-level so e.g. SAN→SANT)
    tokens = re.findall(r"[A-Z]+", base)
    tokens = [CAST2CAT.get(t, t) for t in tokens]
    base_norm = "".join(tokens)

    cands = {base_norm}

    # Pattern: "X (Son)" / "X (so)" / "X (Can)" — also try inverted form
    m = re.search(r"^([A-Z\s.,]+?)\s*\((?:Son|SON|so|SO|Can|CAN|can|CAN)\)",
                  title)
    if m:
        head_tokens = re.findall(r"[A-Z]+", _strip_accents(m.group(1)).upper())
        head_tokens = [CAST2CAT.get(t, t) for t in head_tokens]
        cands.add("SON" + "".join(head_tokens))
        cands.add("CA" + "".join(head_tokens))

    # Pattern: "X (cabo[, …])" → "CABO X" or Catalan "CAP X"
    m = re.search(r"^([A-Z\s.,]+?)\s*\(cabo", title)
    if m:
        head = compress(m.group(1))
        cands.add("CABO" + head)
        cands.add("CAP" + head)

    # Pattern: "X (cala[, …])" → "CALA X"
    m = re.search(r"^([A-Z\s.,]+?)\s*\(cala", title)
    if m:
        head = compress(m.group(1))
        cands.add("CALA" + head)

    # Pattern: 'CAS X' (Mallorquí) — also try 'CA SX' / 'CASX'
    m = re.match(r"^CAS\s+([A-Z]{2,})", base)
    if m:
        cands.add("CAS" + m.group(1))
        cands.add("CA" + m.group(1))
        cands.add("CAN" + m.group(1))

    # Pattern: 'CA X' — also CAN/CA NA prefixes
    m = re.match(r"^CA\s+([A-Z]{2,})", base)
    if m:
        cands.add("CA" + m.group(1))
        cands.add("CAN" + m.group(1))
        cands.add("CANA" + m.group(1))

    return sorted(c for c in cands if c)


def main():
    con = duckdb.connect(str(DB), read_only=True)
    rows = con.execute(
        "SELECT id, vol, leaf, title, place_type, island, municipality "
        "FROM text_entries ORDER BY id"
    ).fetchall()
    con.close()

    gaz = pd.read_parquet(GAZ)
    norm_list = gaz["normalized"].astype(str).tolist()
    print(f"NGIB gazetteer: {len(gaz):,} toponyms, "
          f"{gaz['local_type'].nunique()} local-type categories")
    print(f"text_entries:   {len(rows)}  titles to audit\n")

    # Categorise place_types by expectation
    EXPECT_NGIB = {
        "villa", "ciudad", "lugar", "aldea", "caserío", "casería",
        "isla", "isleta", "islote",
        "cabo", "cala", "bahía", "punta", "torre",
        "río", "rio", "arroyo", "balsa", "fuente",
        "monte", "sierra", "cordillera", "valle",
        "partido judicial", "diócesis", "audiencia", "provincia",
        "feligresía", "parroquia",
        "puerto", "porto",
    }
    MAYBE_NGIB = {
        "predio", "alquería", "casa de campo", "finca",
        "cortijo", "hacienda", "estancia", "porción de terreno",
    }

    matches = []
    no_matches_expect = []
    no_matches_maybe = []
    no_matches_other = []

    for tid, vol, leaf, title, pt, isl, muni in rows:
        cands = normalise_madoz_title(title)
        if not cands:
            continue
        best = None
        for cand in cands:
            # Exact match first
            if cand in norm_list:
                best = (cand, 100, cand)
                break
            # Fuzzy via rapidfuzz process
            top = process.extractOne(cand, norm_list, scorer=fuzz.ratio,
                                     score_cutoff=78)
            if top and (best is None or top[1] > best[1]):
                best = (cand, top[1], top[0])

        pt_low = (pt or "").lower()
        if best:
            matches.append((tid, vol, leaf, title, pt, best))
        else:
            tup = (tid, vol, leaf, title, pt)
            if pt_low in EXPECT_NGIB:
                no_matches_expect.append(tup)
            elif pt_low in MAYBE_NGIB:
                no_matches_maybe.append(tup)
            else:
                no_matches_other.append(tup)

    print(f"=== Match summary ===")
    print(f"  Matched NGIB (≥78 fuzz):  {len(matches):4d}  "
          f"({len(matches)/len(rows)*100:.1f}%)")
    print(f"  No NGIB match:")
    print(f"    EXPECTED to match (settlements / capes / etc.): {len(no_matches_expect)}")
    print(f"    MAYBE in NGIB (predios / alquerías):           {len(no_matches_maybe)}")
    print(f"    OTHER (rare types, missing place_type):        {len(no_matches_other)}\n")

    print("=== Settlements / geographic features WITHOUT NGIB match ===\n")
    if not no_matches_expect:
        print("  (none — all populated nuclei and named features matched)")
    else:
        for tid, vol, leaf, title, pt in no_matches_expect[:60]:
            print(f"  id={tid:5d} v{vol} l{leaf:4d}  [{pt:18s}]  {title!r}")

    if no_matches_maybe:
        print(f"\n=== Predios / alquerías WITHOUT NGIB match (first 20) ===\n")
        for tid, vol, leaf, title, pt in no_matches_maybe[:20]:
            print(f"  id={tid:5d} v{vol} l{leaf:4d}  [{pt:18s}]  {title!r}")


if __name__ == "__main__":
    main()
