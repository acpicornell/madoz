#!/usr/bin/env python3
"""Match each Madoz entry to NGIB coordinates via the gazetteer.

For every entry in data/text/page_*.json we try to find a lat/lon by
fuzzy-matching the title against the Balearic gazetteer
(data/gazetteer.parquet, built by build_gazetteer.py).

Match priority:
    0. curated override: small table of explicit (island, title)→(lon,lat)
       for NGIB-ambiguous cases the article body resolves unambiguously.
    1. exact normalized title against the gazetteer (within the same island
       if declared)
    2. fuzzy match (rapidfuzz WRatio ≥ 88) with safeguards:
         - same-island when the entry declares one
         - length-disparity guard (≥ 60 % of title length) — prevents
           "ROJA" scoring 90 against "ALCARIA ROJA"
         - municipality tiebreaker: prefer rows whose mun matches the
           article's declared parent
    3. matriz fallback: re-match the article's declared parent
       municipality against the gazetteer.
    4. island centroid as last resort, with explicit fallback label so it
       is visually distinguishable from real matches.

Output: writes data/coords.json keyed by (vol, leaf, title) so
``export_web_data.py`` can inject coords into web/data.json.

This is the Madoz-side analogue of minano's enrich_coords.py with the
same six safeguards. The 19th-century gap between Madoz's Castilianised
spellings and NGIB's modern Catalan is bridged by the ``historical``
rows added to the gazetteer.

Run:
    python scripts/enrich_coords.py        # write data/coords.json
    python scripts/enrich_coords.py --stats  # print match-rate stats
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import duckdb
from rapidfuzz import fuzz, process

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from build_gazetteer import normalize  # noqa: E402

GAZETTEER = ROOT / 'data' / 'gazetteer.parquet'
OUT = ROOT / 'data' / 'coords.json'


def load_gazetteer():
    """Returns {island: [{...}, ...]} ready for fuzzy matching."""
    con = duckdb.connect(':memory:')
    rows = con.sql(f"""
        SELECT normalized, lon, lat, spelling, source, municipality, island, local_type
        FROM read_parquet('{GAZETTEER}')
        WHERE (lon IS NOT NULL AND lat IS NOT NULL)
           OR source = 'historical'
    """).fetchall()
    by_island: dict[str, list[dict]] = {}
    for r in rows:
        norm, lon, lat, spelling, source, mun, isl, ltype = r
        if not norm:
            continue
        by_island.setdefault(isl or '(unknown)', []).append({
            'norm': norm, 'lon': lon, 'lat': lat,
            'spelling': spelling, 'source': source,
            'mun': mun, 'island': isl, 'local_type': ltype,
        })
    return by_island


# Madoz writes Ibiza/Iviza; NGIB uses Eivissa.
ISLAND_ALIAS = {
    'Ibiza': 'Eivissa',
    'Iviza': 'Eivissa',
    'Eivissa': 'Eivissa',
    'Mallorca': 'Mallorca',
    'Menorca': 'Menorca',
    'Formentera': 'Formentera',
    'Cabrera': 'Cabrera',
    'Baleares': None,
}

# Curated coordinates for titles whose toponym appears in NGIB under
# multiple homonyms and where the Madoz article body makes the
# intended referent unambiguous even though the string match cannot.
CURATED_OVERRIDES: dict[tuple[str, str], tuple[float, float, str]] = {
    ('Mallorca', 'VILETA'):     (2.6207, 39.5929, 'la Vileta (Palma)'),
    ('Mallorca', 'SON LLUCH'):  (2.7503, 39.5899, 'Son Lluc (Palma)'),
    ('Mallorca', 'SALINAS LAS'): (3.0535, 39.3392, 'ses Salines'),
}

# Entries whose article is too thin to disambiguate among multiple NGIB
# homonyms. Routed to island centroid with a distinct fallback label.
AMBIGUOUS_HOMONYMS: set[tuple[str, str]] = set()

# Island centroid fallbacks (WGS84).
ISLAND_CENTROID = {
    'Mallorca':   (2.92, 39.60),
    'Menorca':    (4.10, 39.95),
    'Eivissa':    (1.43, 38.97),
    'Formentera': (1.45, 38.69),
    'Cabrera':    (2.93, 39.15),
}

# Madoz editorial markers that are not part of the toponym.
_EDITORIAL_SUFFIX_RX = re.compile(
    r'\s*(?:[—–-]\s*)?\((?:adici[oó]n|adiciones|adicion)\)\s*$',
    re.IGNORECASE,
)
_EDITORIAL_TRAIL_RX = re.compile(
    r'\s*[—–-]\s*(?:adici[oó]n(?:es)?|coordenades|estad[ií]sticas?[^)]*)\s*$',
    re.IGNORECASE,
)


def _jitter(lon: float, lat: float, key: str, radius: float = 0.0045) -> tuple[float, float]:
    """Deterministic small offset (~500 m max) for entries that pile up
    at the same point.

    Many Madoz predios share a parent municipality and end up routed to
    the same village centre via the matriz fallback (e.g. 101 entries
    at Pollença, 48 at Santa Margalida, 41 at Maó). Without spreading
    they render as a single dot and the map loses any sense of how
    much detail each muni carries. The jitter is derived from a hash
    of (vol, leaf, title, island) so the same entry always lands at
    the same offset across runs.
    """
    h = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16)
    # Polar sampling so points spread within a disc, not a square.
    import math
    angle = ((h >> 0) & 0xFFFF) / 0xFFFF * 2 * math.pi
    dist = ((h >> 16) & 0xFFFF) / 0xFFFF * radius
    return lon + dist * math.cos(angle), lat + dist * math.sin(angle)


def _strip_editorial(title: str) -> str:
    t = _EDITORIAL_SUFFIX_RX.sub('', title)
    t = _EDITORIAL_TRAIL_RX.sub('', t)
    return t.strip()


def best_match(title: str, island: str | None, gz_by_island: dict,
               entry_municipality: str | None = None):
    norm_title = normalize(_strip_editorial(title))
    if not norm_title or len(norm_title) < 3:
        return None

    island_ngib0 = ISLAND_ALIAS.get(island, island)

    if (island_ngib0, norm_title) in AMBIGUOUS_HOMONYMS:
        return {'_ambiguous_homonym': True}

    override = CURATED_OVERRIDES.get((island_ngib0, norm_title))
    if override:
        lon, lat, label = override
        return {'lon': lon, 'lat': lat, 'matched': label,
                'score': 100, 'curated': True}

    pools = []
    if island_ngib0 and island_ngib0 in gz_by_island:
        pools.append(gz_by_island[island_ngib0])
    else:
        pools.append([r for rows in gz_by_island.values() for r in rows])

    entry_mun_norm = normalize(entry_municipality) if entry_municipality else ''

    def resolve_to_coords(r, score, pool):
        if r['lon'] is not None and r['lat'] is not None:
            return {'lon': r['lon'], 'lat': r['lat'],
                    'matched': r['spelling'], 'score': round(score, 1)}
        mun_target = r.get('mun')
        if not mun_target:
            return None
        for r2 in pool:
            if r2.get('spelling') == mun_target and r2['lon'] is not None:
                return {'lon': r2['lon'], 'lat': r2['lat'],
                        'matched': r2['spelling'], 'score': round(score, 1)}
        norm_mun = normalize(mun_target)
        choices2 = [r2['norm'] for r2 in pool if r2['lon'] is not None]
        meta2 = [r2 for r2 in pool if r2['lon'] is not None]
        if not choices2:
            return None
        result = process.extractOne(norm_mun, choices2, scorer=fuzz.WRatio,
                                    score_cutoff=85)
        if result:
            _, s2, idx2 = result
            r2 = meta2[idx2]
            return {'lon': r2['lon'], 'lat': r2['lat'],
                    'matched': r2['spelling'],
                    'score': round(min(score, s2), 1),
                    'via_modern': mun_target}
        return None

    def municipality_match(r):
        if not entry_mun_norm:
            return False
        mun_norm = normalize(r.get('mun') or '')
        if not mun_norm:
            return False
        if mun_norm == entry_mun_norm:
            return True
        # Loose match: 'Maria' ↔ 'Maria de la Salut', 'Sant Llorenç' ↔
        # 'Sant Llorenç des Cardassar'. Substring on word boundary.
        if entry_mun_norm in mun_norm.split() + [mun_norm.split()[0] if mun_norm else '']:
            return True
        return entry_mun_norm == mun_norm.split()[0] if mun_norm else False

    # Predio-type matches in the *wrong* municipality are almost always
    # homonym mismatches (every Mallorquin valley has its own Son X
    # farms; NGIB carries 19 distinct toponyms containing "Monjo", so
    # picking the Son Monjo of Montuïri for an article describing the
    # Son Monjo of Campanet is the dominant failure mode). When the
    # article declares a parent municipality and the candidate is one
    # of these farm/agroindustrial types in a different muni, reject
    # the match so the via_matriz fallback (parent village coordinates)
    # wins instead.
    FARM_TYPES = ('Finca', 'Construcció agroindustrial', 'Construcció')

    def acceptable(r):
        if not entry_mun_norm:
            return True
        ltype = r.get('local_type') or ''
        if not any(t in ltype for t in FARM_TYPES):
            return True
        return municipality_match(r)

    for pool in pools:
        exact = [r for r in pool if r['norm'] == norm_title]
        if exact:
            exact.sort(key=lambda r: (
                not municipality_match(r),
                r['source'] != 'historical',
                'Municipi' not in (r.get('local_type') or ''),
            ))
            cand = exact[0]
            if acceptable(cand):
                res = resolve_to_coords(cand, 100, pool)
                if res:
                    return res

        min_len = max(4, int(len(norm_title) * 0.6))
        choices_filtered = [
            (i, r['norm']) for i, r in enumerate(pool)
            if len(r['norm']) >= min_len
        ]
        if choices_filtered:
            idx_map = [i for i, _ in choices_filtered]
            choices = [c for _, c in choices_filtered]
            results = process.extract(
                norm_title, choices, scorer=fuzz.WRatio,
                score_cutoff=88, limit=10,
            )
            if results:
                results.sort(key=lambda t: (
                    not municipality_match(pool[idx_map[t[2]]]),
                    -t[1],
                ))
                # Walk in sorted order, skip unacceptable (wrong-muni
                # farm) candidates. If everything in the pool is a
                # wrong-muni farm, fall through to None so the matriz
                # fallback runs.
                for _, score, local_idx in results:
                    r = pool[idx_map[local_idx]]
                    if not acceptable(r):
                        continue
                    res = resolve_to_coords(r, score, pool)
                    if res:
                        return res
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stats', action='store_true',
                    help='print match-rate stats only, do not write')
    args = ap.parse_args()

    print('Loading gazetteer…', file=sys.stderr)
    gz = load_gazetteer()
    total_gz = sum(len(v) for v in gz.values())
    print(f'  {total_gz:,} gazetteer rows across {len(gz)} islands', file=sys.stderr)

    coords = []
    n_total = 0
    n_matched = 0
    n_via_matriz = 0
    n_centroid = 0
    n_ambiguous = 0
    missed = []

    # Read entries from the DB rather than the per-leaf JSON: the DB is
    # the authoritative source after fill_missing_islands.py /
    # fix_title_mismatches.py / recover_*.py have run. The JSON files
    # are sometimes stale (e.g. JOSE (San) leaf 09/646 has
    # island='Menorca' in the JSON but the DB correctly has the second
    # row as Ibiza). Geocoding from stale JSON places homonyms on the
    # wrong island.
    DB = ROOT / 'db' / 'madoz.duckdb'
    con = duckdb.connect(str(DB), read_only=True)
    rows = con.execute(
        """SELECT vol, leaf, title, island, municipality
           FROM text_entries
           WHERE title IS NOT NULL
           ORDER BY vol, leaf, title, island"""
    ).fetchall()

    for vol, leaf, title, island, mat in rows:
        n_total += 1

        def emit(extra, _vol=vol, _leaf=leaf, _title=title, _island=island):
            # ``island`` is part of the key so that two same-leaf
            # same-title homonyms ('CONSELL' Mallorca + 'CONSELL'
            # Menorca on leaf 06/571) keep separate coordinates.
            coords.append({
                'vol': _vol, 'leaf': int(_leaf),
                'title': _title, 'island': _island, **extra,
            })

        match = best_match(title, island, gz, entry_municipality=mat)
        if match and not match.get('_ambiguous_homonym'):
            emit(match)
            n_matched += 1
            continue

        if match and match.get('_ambiguous_homonym'):
            island_ngib = ISLAND_ALIAS.get(island)
            if island_ngib and island_ngib in ISLAND_CENTROID:
                lon, lat = ISLAND_CENTROID[island_ngib]
                emit({
                    'lon': lon, 'lat': lat,
                    'matched': f'(ubicació indeterminada · {island_ngib})',
                    'score': 0,
                    'fallback': 'ambiguous-homonym',
                })
                n_matched += 1
                n_ambiguous += 1
                continue

        if mat:
            match = best_match(mat, island, gz)
            if match and not match.get('_ambiguous_homonym'):
                match['via_matriz'] = mat
                # Spread same-village entries with a small deterministic
                # jitter so 50+ predios of Pollença / Santa Margalida /
                # Maó don't render as a single overlap.
                if match.get('lon') is not None and match.get('lat') is not None:
                    key = f"{vol}/{leaf}/{title}/{island or ''}"
                    match['lon'], match['lat'] = _jitter(
                        match['lon'], match['lat'], key
                    )
                emit(match)
                n_matched += 1
                n_via_matriz += 1
                continue

        island_ngib = ISLAND_ALIAS.get(island)
        if island_ngib and island_ngib in ISLAND_CENTROID:
            lon, lat = ISLAND_CENTROID[island_ngib]
            emit({
                'lon': lon, 'lat': lat,
                'matched': f'(centroide {island_ngib})',
                'score': 0,
                'fallback': 'island-centroid',
            })
            n_matched += 1
            n_centroid += 1
            continue

        missed.append((title, island))

    print(f'\n=== match rate ===', file=sys.stderr)
    print(f'  exact / fuzzy:        {n_matched - n_via_matriz - n_centroid - n_ambiguous:>4}', file=sys.stderr)
    print(f'  via parent matriz:    {n_via_matriz:>4}', file=sys.stderr)
    print(f'  ambiguous-homonym:    {n_ambiguous:>4}', file=sys.stderr)
    print(f'  island-centroid:      {n_centroid:>4}', file=sys.stderr)
    print(f'  TOTAL placed:         {n_matched:>4} / {n_total}  '
          f'({n_matched * 100 / n_total:.1f}%)', file=sys.stderr)
    print(f'  unmatched:            {len(missed):>4}', file=sys.stderr)

    if missed and args.stats:
        print(f'\nFirst 20 unmatched:', file=sys.stderr)
        for title, island in missed[:20]:
            print(f'    {title!r:40s} [{island}]', file=sys.stderr)

    if not args.stats:
        OUT.write_text(json.dumps(coords, indent=2))
        print(f'\nWrote {len(coords)} coord rows → {OUT.relative_to(ROOT)}', file=sys.stderr)


if __name__ == '__main__':
    main()
