#!/usr/bin/env python3
"""Construct a fuzzy-searchable Balearic gazetteer from NGIB.

Source: NGIB (Nomenclàtor Geogràfic de les Illes Balears) — ~55,500
toponyms with municipality, island, local_type and coordinates, plus
~1,400 variant spellings. Fetched by ``scripts/fetch_ngib.py``.

Output: ``data/gazetteer.parquet`` with rows:

    - id                 NGIB geographic_name_id
    - spelling           original spelling (Catalan modern)
    - normalized         article-stripped, accent-stripped, uppercase
    - tokens             space-separated tokens of normalized
    - first_token        first token (after dropping the article)
    - municipality       Catalan modern (e.g. "Pollença")
    - island             Mallorca / Menorca / Eivissa / Formentera / Cabrera
    - local_type         from NGIB taxonomy
    - is_settlement      bool — likely matches a Madoz entry
    - lon, lat           coordinates (WGS84)
    - source             'ngib' | 'historical'

The ``historical`` rows are hand-curated 19th-century Castilian/
Catalan spellings that won't appear in NGIB's modern Catalan
lemmata — they're the bridge between Madoz's typesetting (1845–1850)
and the NGIB modern lemma. Most overlap with what was needed for the
Miñano project (the two corpora use very similar Castilianised
forms); a small Madoz-specific addendum at the end of
``HISTORICAL_VARIANTS`` covers the typos Madoz introduced that
Miñano did not (``Santagny`` for Santanyí, ``Monacor`` for Manacor,
the broken accent in ``Mahon`` rendered ``Manon`` by the OCR, etc.).
"""
from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
NGIB_DATA = ROOT / 'data' / 'ngib'

SETTLEMENT_TYPES = {
    'Municipi',
    'Capital de municipi',
    'Capital de Municipi',
    'Nucli de població capital de municipi',
    'Entitat de Població',
    'Llogaret, llogarret, ranxo',
    'Altre nucli de població, llogaret',
    'Vila',
    'Barri',
    'Barriada',
    'Urbanització, barriada (aïllat)',
}

# Authoritative type priority for resolving homonym collisions within
# a single island. When two NGIB rows normalise to the same name on
# the same island, the higher-priority type wins the dedup.
LOCAL_TYPE_PRIORITY = [
    'Municipi',
    'Capital de municipi',
    'Capital de Municipi',
    'Nucli de població capital de municipi',
    'Vila',
    'Entitat de Població',
    'Llogaret, llogarret, ranxo',
    'Altre nucli de població, llogaret',
    'Illa gran',
    'Illa mitjana',
    'Urbanització, barriada (aïllat)',
    'Barriada',
    'Barri',
    'Santuari',
    'Monestir, convent, cartoixa',
    'Església, capella, oratori, ermita',
    'Edifici religiós',
    'Castell, fortalesa',
    'Far',
    'Cim, puig, talaia',
    'Elevació gran',
    'Serra, serral, serralada',
    'Cap, punta, morro mitjà',
    'Cap, punta, morro petit',
    'Estret, cala, badia mitjana',
    'Estret, cala petita, rada',
    'Construcció agroindustrial',
    'Finca, possessió, lloc, casa pagesa, caseta',
    'Accident petit, relleu del fons marí, illot',
    'Monument',
]


def _type_rank(ltype: str | None) -> int:
    try:
        return LOCAL_TYPE_PRIORITY.index(ltype or '')
    except ValueError:
        return len(LOCAL_TYPE_PRIORITY)


POSSESSION_TYPES = {
    'Finca, possessió, lloc, casa pagesa, caseta',
    'Construcció agroindustrial',
}
RELIGIOUS_TYPES = {
    'Edifici religiós',
    'Església, capella, oratori, ermita',
    'Monestir, convent, cartoixa',
    'Santuari',
    'Monument',
}
NATURAL_TYPES = {
    'Cim, puig, talaia',
    'Pic, cim petit (puntual)',
    'Elevació petita',
    'Elevació gran',
    'Serra, serral, serralada',
    'Cap, punta, morro mitjà',
    'Cap, punta, morro petit',
    'Estret, cala, badia mitjana',
    'Estret, cala petita, rada',
    'Cova, balma, avenc',
    'Torrent',
    'Font, surgència',
    'Illa gran',
    'Illa mitjana',
    'Accident petit, relleu del fons marí, illot',
    'Pla, plana',
    'Castell, fortalesa',
    'Far',
}
ALL_TYPES = SETTLEMENT_TYPES | POSSESSION_TYPES | RELIGIOUS_TYPES | NATURAL_TYPES

LEADING_ARTICLES = (
    "S'", "s'", "L'", "l'",
    "es ", "Es ", "ES ",
    "sa ", "Sa ", "SA ",
    "el ", "El ", "EL ",
    "la ", "La ", "LA ",
    "els ", "Els ", "ELS ",
    "les ", "Les ", "LES ",
    "ses ", "Ses ", "SES ",
    "sos ", "Sos ", "SOS ",
    "so ", "So ",
    "na ", "Na ",
    "en ", "En ", "n'", "N'",
)


def strip_diacritics(s: str) -> str:
    n = unicodedata.normalize('NFD', s)
    return ''.join(c for c in n if unicodedata.category(c) != 'Mn')


def strip_article(s: str) -> str:
    for a in LEADING_ARTICLES:
        if s.startswith(a):
            return s[len(a):]
    return s


def normalize(s: str) -> str:
    """Uppercase, strip accents, strip article, collapse whitespace."""
    if not s:
        return ''
    s = strip_article(s.strip())
    s = strip_diacritics(s)
    s = s.upper()
    for sep in ('-', '–', '—', '/'):
        s = s.replace(sep, ' ')
    s = ' '.join(s.split())
    for ch in '.,;:¡¿!?()[]{}«»"\'`':
        s = s.replace(ch, '')
    return s.strip()


# Hand-curated 19th-century Castilian/Catalan spellings seen in
# Madoz's text that won't appear in NGIB.  (spelling, modern_form, island)
HISTORICAL_VARIANTS = [
    ("Mallorca",   "Mallorca",   "Mallorca"),
    ("Mallorea",   "Mallorca",   "Mallorca"),
    ("Maiorca",    "Mallorca",   "Mallorca"),
    ("Mahón",      "Maó",        "Menorca"),
    ("Mahon",      "Maó",        "Menorca"),
    ("Manon",      "Maó",        "Menorca"),
    ("Iviza",      "Eivissa",    "Eivissa"),
    ("Ibiza",      "Eivissa",    "Eivissa"),
    ("Pollensa",   "Pollença",   "Mallorca"),
    ("Pollenza",   "Pollença",   "Mallorca"),
    ("Pollentia",  "Pollença",   "Mallorca"),
    ("Felanitx",   "Felanitx",   "Mallorca"),
    ("Lluchmayor", "Llucmajor",  "Mallorca"),
    ("Llumayor",   "Llucmajor",  "Mallorca"),
    ("Llucmajor",  "Llucmajor",  "Mallorca"),
    ("Andraitx",   "Andratx",    "Mallorca"),
    ("Andraix",    "Andratx",    "Mallorca"),
    ("Andraig",    "Andratx",    "Mallorca"),
    ("Andrach",    "Andratx",    "Mallorca"),
    ("Andrache",   "Andratx",    "Mallorca"),
    ("Bunyola",    "Bunyola",    "Mallorca"),
    ("Buñola",     "Bunyola",    "Mallorca"),
    ("Bañola",     "Bunyola",    "Mallorca"),
    ("Sineu",      "Sineu",      "Mallorca"),
    ("Sinen",      "Sineu",      "Mallorca"),
    ("Manacor",    "Manacor",    "Mallorca"),
    ("Monacor",    "Manacor",    "Mallorca"),
    ("Selva",      "Selva",      "Mallorca"),
    ("Inca",       "Inca",       "Mallorca"),
    ("Soller",     "Sóller",     "Mallorca"),
    ("Söller",     "Sóller",     "Mallorca"),
    ("Esporlas",   "Esporles",   "Mallorca"),
    ("Esporles",   "Esporles",   "Mallorca"),
    ("Valldemosa", "Valldemossa","Mallorca"),
    ("Valldemusa", "Valldemossa","Mallorca"),
    ("Valdemosa",  "Valldemossa","Mallorca"),
    ("Estellenchs","Estellencs", "Mallorca"),
    ("Establiments","Establiments","Mallorca"),
    ("Establimens","Establiments","Mallorca"),
    ("Puigpunyent","Puigpunyent","Mallorca"),
    ("Puigpuñent", "Puigpunyent","Mallorca"),
    ("Puigpuñer",  "Puigpunyent","Mallorca"),
    ("Marratchi",  "Marratxí",   "Mallorca"),
    ("Marratxi",   "Marratxí",   "Mallorca"),
    ("Santañy",    "Santanyí",   "Mallorca"),
    ("Santany",    "Santanyí",   "Mallorca"),
    ("Santagny",   "Santanyí",   "Mallorca"),   # Madoz typo
    ("Santanyí",   "Santanyí",   "Mallorca"),
    ("Calviá",     "Calvià",     "Mallorca"),
    ("Calvià",     "Calvià",     "Mallorca"),
    ("Banyalbufar","Banyalbufar","Mallorca"),
    ("Bañalbufar", "Banyalbufar","Mallorca"),
    ("Deyá",       "Deià",       "Mallorca"),
    ("Deyà",       "Deià",       "Mallorca"),
    ("Llubin",     "Llubí",      "Mallorca"),
    ("Llubí",      "Llubí",      "Mallorca"),
    ("Lloseta",    "Lloseta",    "Mallorca"),
    ("Llorito",    "Lloret de Vistalegre","Mallorca"),
    ("Lloret",     "Lloret de Vistalegre","Mallorca"),
    ("Algaida",    "Algaida",    "Mallorca"),
    ("Algayda",    "Algaida",    "Mallorca"),
    ("Caymari",    "Caimari",    "Mallorca"),
    ("Caimari",    "Caimari",    "Mallorca"),
    ("Mancor",     "Mancor de la Vall","Mallorca"),
    ("Moscari",    "Moscari",    "Mallorca"),
    ("Moscarí",    "Moscari",    "Mallorca"),
    ("Biniamar",   "Biniamar",   "Mallorca"),
    ("Binisalem",  "Binissalem", "Mallorca"),
    ("Bisanlem",   "Binissalem", "Mallorca"),
    ("Benisalem",  "Binissalem", "Mallorca"),    # Madoz uses BENISALEM
    ("Búger",      "Búger",      "Mallorca"),
    ("Buger",      "Búger",      "Mallorca"),
    ("Bugeu",      "Búger",      "Mallorca"),
    ("Belver",     "Bellver",    "Mallorca"),
    ("Bellver",    "Bellver",    "Mallorca"),
    ("Belver Castillo","Castell de Bellver","Mallorca"),
    ("Ariañy",     "Ariany",     "Mallorca"),
    ("Ariany",     "Ariany",     "Mallorca"),
    ("Costiche",   "Costitx",    "Mallorca"),
    ("Costítx",    "Costitx",    "Mallorca"),
    ("Costitx",    "Costitx",    "Mallorca"),
    ("Consell",    "Consell",    "Mallorca"),
    ("Petra",      "Petra",      "Mallorca"),
    ("Sancellas",  "Sencelles",  "Mallorca"),
    ("Sencelles",  "Sencelles",  "Mallorca"),
    ("Sansellas",  "Sencelles",  "Mallorca"),
    ("Sancelles",  "Sencelles",  "Mallorca"),
    ("Vilafranca", "Vilafranca de Bonany","Mallorca"),
    ("Villafranca","Vilafranca de Bonany","Mallorca"),
    ("Sa Pobla",   "sa Pobla",   "Mallorca"),
    ("La Puebla",  "sa Pobla",   "Mallorca"),
    ("Puebla",     "sa Pobla",   "Mallorca"),
    ("Pobla",      "sa Pobla",   "Mallorca"),
    ("Sant Joan",  "Sant Joan",  "Mallorca"),
    ("San Joan",   "Sant Joan",  "Mallorca"),
    ("San Juan",   "Sant Joan",  "Mallorca"),
    ("Son Servera","Son Servera","Mallorca"),
    ("Sonservera", "Son Servera","Mallorca"),
    ("Sant Llorenç","Sant Llorenç des Cardassar","Mallorca"),
    ("San Lorenzo del Cardasar","Sant Llorenç des Cardassar","Mallorca"),
    ("San Lorenzo ó Llorens Descardasar","Sant Llorenç des Cardassar","Mallorca"),
    ("Capdepera",  "Capdepera",  "Mallorca"),
    ("Cap de Pera","Capdepera",  "Mallorca"),
    ("Alcaria-Roja",   "Alqueria Roja",      "Mallorca"),
    ("Alcaria Roja",   "Alqueria Roja",      "Mallorca"),
    ("Alcaria-Blanca", "s'Alqueria Blanca",  "Mallorca"),
    ("Alcaria Blanca", "s'Alqueria Blanca",  "Mallorca"),
    ("Artá",       "Artà",       "Mallorca"),
    ("Artà",       "Artà",       "Mallorca"),
    ("Porreras",   "Porreres",   "Mallorca"),
    ("Porreres",   "Porreres",   "Mallorca"),
    ("Alcudia",    "Alcúdia",    "Mallorca"),
    ("Alcúdia",    "Alcúdia",    "Mallorca"),
    ("Alcudía",    "Alcúdia",    "Mallorca"),
    ("Montuiri",   "Montuïri",   "Mallorca"),
    ("Muro",       "Muro",       "Mallorca"),
    ("María",      "Maria de la Salut","Mallorca"),
    ("Maria de la Salud","Maria de la Salut","Mallorca"),
    ("Santa María","Santa Maria del Camí","Mallorca"),
    ("Santa Maria","Santa Maria del Camí","Mallorca"),
    ("Santa Margarita","Santa Margalida","Mallorca"),
    ("Santa Margalida","Santa Margalida","Mallorca"),
    ("Santa Eugenia","Santa Eugènia","Mallorca"),
    ("Felanitx",   "Felanitx",   "Mallorca"),
    ("Fornalutx",  "Fornalutx",  "Mallorca"),
    ("Fornaluche", "Fornalutx",  "Mallorca"),
    ("Llucalcari", "Llucalcari", "Mallorca"),
    ("Lluch",      "Lluc",       "Mallorca"),
    ("Lluc",       "Lluc",       "Mallorca"),
    ("Randa",      "Randa",      "Mallorca"),
    # Menorca
    ("Mahón",      "Maó",        "Menorca"),
    ("Maó",        "Maó",        "Menorca"),
    ("Ciudadela",  "Ciutadella de Menorca","Menorca"),
    ("Ciutadella", "Ciutadella de Menorca","Menorca"),
    ("Alaior",     "Alaior",     "Menorca"),
    ("Alayor",     "Alaior",     "Menorca"),
    ("Mercadal",   "es Mercadal","Menorca"),
    ("Ferrerías",  "Ferreries",  "Menorca"),
    ("Ferreries",  "Ferreries",  "Menorca"),
    ("Perrerías",  "Ferreries",  "Menorca"),
    ("Fornells",   "Fornells",   "Menorca"),
    ("San Cristóbal","es Migjorn Gran","Menorca"),
    ("San Cristobal","es Migjorn Gran","Menorca"),
    ("San Climent","Sant Climent","Menorca"),
    ("San Clemente","Sant Climent","Menorca"),
    ("San Luis",   "Sant Lluís", "Menorca"),
    ("Sant Lluís", "Sant Lluís", "Menorca"),
    ("Villacarlos","es Castell", "Menorca"),
    ("Villa Carlos","es Castell","Menorca"),
    ("Es Castell", "es Castell", "Menorca"),
    ("Adaya",      "Addaia",     "Menorca"),
    # Eivissa / Ibiza
    ("Iviza",      "Eivissa",    "Eivissa"),
    ("Ibiza",      "Eivissa",    "Eivissa"),
    ("Eivissa",    "Eivissa",    "Eivissa"),
    ("Sant Antoni","Sant Antoni de Portmany","Eivissa"),
    ("San Antonio","Sant Antoni de Portmany","Eivissa"),
    ("Pormany",    "Sant Antoni de Portmany","Eivissa"),
    ("Portmany",   "Sant Antoni de Portmany","Eivissa"),
    ("Sant Josep", "Sant Josep de sa Talaia","Eivissa"),
    ("San José",   "Sant Josep de sa Talaia","Eivissa"),
    ("Sant Joan",  "Sant Joan de Labritja","Eivissa"),
    ("San Juan Bautista","Sant Joan de Labritja","Eivissa"),
    ("Sant Carles","Sant Carles de Peralta","Eivissa"),
    ("San Carlos", "Sant Carles de Peralta","Eivissa"),
    ("Santa Eulalia","Santa Eulària des Riu","Eivissa"),
    ("Santa Eulària","Santa Eulària des Riu","Eivissa"),
    ("Sant Llorenç","Sant Llorenç de Balàfia","Eivissa"),
    ("San Lorenzo","Sant Llorenç de Balàfia","Eivissa"),
    ("Sant Rafel", "Sant Rafel de sa Creu","Eivissa"),
    ("San Rafael", "Sant Rafel de sa Creu","Eivissa"),
    ("Santa Gertrudis","Santa Gertrudis de Fruitera","Eivissa"),
    ("Santa Inés", "Santa Agnès de Corona","Eivissa"),
    ("Santa Agnès","Santa Agnès de Corona","Eivissa"),
    ("Sant Jordi", "Sant Jordi de Ses Salines","Eivissa"),
    ("San Jorge",  "Sant Jordi de Ses Salines","Eivissa"),
    ("Jesús",      "Jesús",      "Eivissa"),
    ("Balanzat",   "Sant Miquel de Balansat","Eivissa"),
    ("Balansat",   "Sant Miquel de Balansat","Eivissa"),
    # Formentera
    ("Formentera", "Formentera", "Formentera"),
    ("San Francisco Javier","Sant Francesc Xavier","Formentera"),
    ("Sant Francesc","Sant Francesc Xavier","Formentera"),
    ("San Fernando","Sant Ferran de ses Roques","Formentera"),
    ("Sant Ferran","Sant Ferran de ses Roques","Formentera"),
    ("Pilar de la Mola","el Pilar de la Mola","Formentera"),
    # Cabrera
    ("Cabrera",    "Cabrera",    "Cabrera"),
]


def main():
    con = duckdb.connect(':memory:')
    types_sql = ', '.join(f"'{t}'" for t in sorted(ALL_TYPES))

    print(f'Loading NGIB main toponyms (types: {len(ALL_TYPES)})…', file=sys.stderr)
    ngib_rows = con.sql(f"""
        SELECT
          spelling, municipality, island, local_type_name,
          geographic_name_id, lon, lat
        FROM read_parquet('{NGIB_DATA}/ngib.parquet')
        WHERE local_type_name IN ({types_sql})
          AND spelling IS NOT NULL
          AND length(spelling) >= 3
          AND status = 'vigent'
    """).fetchall()
    print(f'  → {len(ngib_rows):,} primary toponyms', file=sys.stderr)

    print('Loading NGIB variants…', file=sys.stderr)
    variant_rows = con.sql(f"""
        SELECT v.spelling, v.municipality, n.island, n.local_type_name,
               v.geographic_name_id, n.lon, n.lat
        FROM read_parquet('{NGIB_DATA}/ngib_variants.parquet') v
        LEFT JOIN read_parquet('{NGIB_DATA}/ngib.parquet') n
          ON v.geographic_name_id = n.geographic_name_id
        WHERE v.spelling IS NOT NULL AND length(v.spelling) >= 3
    """).fetchall()
    print(f'  → {len(variant_rows):,} variant spellings', file=sys.stderr)

    settlement_set = SETTLEMENT_TYPES | POSSESSION_TYPES

    all_rows = ngib_rows + variant_rows
    all_rows.sort(key=lambda r: (
        normalize(r[0] or '') or '~',
        r[2] or '',
        _type_rank(r[3]),
    ))

    out_rows = []
    seen_norm: set[tuple[str, str]] = set()

    for (spelling, mun, isl, ltype, gn_id, lon, lat) in all_rows:
        if not spelling or not isl:
            continue
        norm = normalize(spelling)
        if not norm or len(norm) < 3:
            continue
        key = (norm, isl)
        if key in seen_norm:
            continue
        seen_norm.add(key)
        tokens = norm.split()
        first_tok = tokens[0] if tokens else ''
        out_rows.append({
            'id': str(gn_id) if gn_id else '',
            'spelling': spelling,
            'normalized': norm,
            'tokens': ' '.join(tokens),
            'first_token': first_tok,
            'municipality': mun or '',
            'island': isl,
            'local_type': ltype or '',
            'is_settlement': ltype in settlement_set,
            'lon': float(lon) if lon and lon != 'None' else None,
            'lat': float(lat) if lat and lat != 'None' else None,
            'source': 'ngib',
        })

    print(f'After dedupe: {len(out_rows):,} NGIB rows', file=sys.stderr)

    for (hist, modern, isl) in HISTORICAL_VARIANTS:
        norm = normalize(hist)
        if not norm or len(norm) < 3:
            continue
        if (norm, isl) in seen_norm:
            continue
        seen_norm.add((norm, isl))
        tokens = norm.split()
        out_rows.append({
            'id': f'hist:{hist}',
            'spelling': hist,
            'normalized': norm,
            'tokens': ' '.join(tokens),
            'first_token': tokens[0] if tokens else '',
            'municipality': modern,
            'island': isl,
            'local_type': 'Variant històrica',
            'is_settlement': True,
            'lon': None, 'lat': None,
            'source': 'historical',
        })

    print(f'Total gazetteer entries: {len(out_rows):,}', file=sys.stderr)

    out_path = ROOT / 'data' / 'gazetteer.parquet'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute("""
        CREATE TABLE out (
            id              VARCHAR,
            spelling        VARCHAR,
            normalized      VARCHAR,
            tokens          VARCHAR,
            first_token     VARCHAR,
            municipality    VARCHAR,
            island          VARCHAR,
            local_type      VARCHAR,
            is_settlement   BOOLEAN,
            lon             DOUBLE,
            lat             DOUBLE,
            source          VARCHAR
        )
    """)
    cols = ['id','spelling','normalized','tokens','first_token','municipality',
            'island','local_type','is_settlement','lon','lat','source']
    con.executemany(
        f"INSERT INTO out VALUES ({', '.join('?' * len(cols))})",
        [[r[c] for c in cols] for r in out_rows],
    )
    con.sql(f"COPY (SELECT * FROM out) TO '{out_path}' (FORMAT PARQUET)")
    print(f'Wrote {out_path}', file=sys.stderr)

    print('\n=== gazetteer breakdown ===', file=sys.stderr)
    for isl, n in con.sql(
        "SELECT island, count(*) FROM out GROUP BY island ORDER BY count(*) DESC"
    ).fetchall():
        print(f'  {isl or "(unknown)":<14} {n:>6,}', file=sys.stderr)
    print(file=sys.stderr)
    for src, n in con.sql(
        "SELECT source, count(*) FROM out GROUP BY source"
    ).fetchall():
        print(f'  source={src:<12} {n:>6,}', file=sys.stderr)
    print(file=sys.stderr)
    for flag, n in con.sql(
        "SELECT is_settlement, count(*) FROM out GROUP BY is_settlement"
    ).fetchall():
        print(f'  is_settlement={str(flag):<6} {n:>6,}', file=sys.stderr)


if __name__ == '__main__':
    main()
