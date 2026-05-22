#!/usr/bin/env python3
"""Download the NGIB (Nomenclàtor Geogràfic de les Illes Balears).

Official IDEIB ArcGIS service:
    https://ideib.caib.es/geoserveis/rest/services/public/NGIB/MapServer/

Layers used:
    0   →  ~55,500 georeferenced toponyms (preferred forms)
    2   →   ~1,400 alternative spellings / variants
    6   →      13 source catalog
    15  →      72 local-type catalog

The four layers are inline-joined and persisted as parquet under
``data/ngib/`` so downstream code (``build_gazetteer.py``,
``enrich_coords.py``, ``suspicious_titles_audit.py``) doesn't need to
re-join on every read.

Usage:
    python scripts/fetch_ngib.py            # download all 4 layers
    python scripts/fetch_ngib.py --check    # print stats for cached files
    python scripts/fetch_ngib.py --force    # overwrite cached files

Idempotent: skips files already present unless --force.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
OUTDIR = PROJECT / "data" / "ngib"

UA = "Mozilla/5.0 (research; madoz-gazetteer)"
PAGE = 1000  # service maxRecordCount

SERVICES = {
    "main":         "https://ideib.caib.es/geoserveis/rest/services/public/NGIB/MapServer/0/query",
    "variants":     "https://ideib.caib.es/geoserveis/rest/services/public/NGIB/MapServer/2/query",
    "sources":      "https://ideib.caib.es/geoserveis/rest/services/public/NGIB/MapServer/6/query",
    "local_types":  "https://ideib.caib.es/geoserveis/rest/services/public/NGIB/MapServer/15/query",
}

ISLAND_NAME = {1: "Mallorca", 2: "Menorca", 3: "Eivissa",
               4: "Formentera", 5: "Cabrera", 9999: "(unassigned)"}
STATUS_NAME = {1: "vigent", 4: "retirada pendent", 5: "modificació pendent"}


def _http_json(url: str, params: dict) -> dict:
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _normalize_article(s: str | None) -> str | None:
    """Lowercase the leading Catalan article ('Es Castell' → 'es Castell')."""
    if not s:
        return s
    for art in ("Es ", "Sa ", "El ", "La ", "Els ", "Les "):
        if s.startswith(art):
            return art.lower() + s[len(art):]
    return s


def _paged(url: str, out_fields: str, want_geom: bool):
    """Yield each `features` object across all pages of a layer."""
    offset = 0
    page = 0
    while True:
        page += 1
        params = {
            "where": "1=1",
            "outFields": out_fields,
            "returnGeometry": "true" if want_geom else "false",
            "outSR": "4326",
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": PAGE,
        }
        try:
            data = _http_json(url, params)
        except Exception as e:
            print(f"  ! page {page} offset={offset}: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(2)
            continue
        feats = data.get("features", [])
        if not feats:
            break
        for f in feats:
            yield f
        exceeded = data.get("exceededTransferLimit", False)
        print(f"  page {page:>3} offset={offset:>6}: {len(feats):>4} features"
              f"{' (more)' if exceeded else ''}", file=sys.stderr)
        if not exceeded and len(feats) < PAGE:
            break
        offset += PAGE
        time.sleep(0.2)


def fetch_catalog(layer: str, out_fields: str) -> list[dict]:
    params = {
        "where": "1=1",
        "outFields": out_fields,
        "returnGeometry": "false",
        "f": "json",
        "resultRecordCount": PAGE,
    }
    data = _http_json(SERVICES[layer], params)
    return [f.get("attributes", {}) for f in data.get("features", [])]


def fetch_main(local_types_map, sources_map) -> list[dict]:
    rows = []
    for f in _paged(
        SERVICES["main"],
        out_fields=("GRAFIA,MUNICIPI,ILLA,TIPUS_LOCAL,ESTAT_NG,FONT,"
                    "OBSERV_NG,OBSERVACIONS,INSPIRE_ID,"
                    "NOM_GEOGRAFIC,NOM_GEOGRAFIC_ID"),
        want_geom=True,
    ):
        a = f.get("attributes", {}) or {}
        g = f.get("geometry") or {}
        spelling = a.get("GRAFIA")
        if not spelling:
            continue
        ic = a.get("ILLA")
        sc = a.get("ESTAT_NG")
        lt = a.get("TIPUS_LOCAL")
        src = a.get("FONT")
        lt_name, lt_def = local_types_map.get(lt, (None, None))
        src_name, src_org = sources_map.get(src, (None, None))
        rows.append({
            "spelling":           _normalize_article(spelling.strip()),
            "municipality":       _normalize_article(a.get("MUNICIPI")),
            "island_code":        ic,
            "island":             ISLAND_NAME.get(ic),
            "local_type":         lt,
            "local_type_name":    lt_name,
            "local_type_definition": lt_def,
            "status_code":        sc,
            "status":             STATUS_NAME.get(sc),
            "source":             src,
            "source_name":        src_name,
            "source_organisme":   src_org,
            "ng_observations":    a.get("OBSERV_NG"),
            "observations":       a.get("OBSERVACIONS"),
            "inspire_id":         a.get("INSPIRE_ID"),
            "geographic_name":    a.get("NOM_GEOGRAFIC"),
            "geographic_name_id": a.get("NOM_GEOGRAFIC_ID"),
            "lon":                g.get("x"),
            "lat":                g.get("y"),
        })
    return rows


def fetch_variants() -> list[dict]:
    rows = []
    for f in _paged(
        SERVICES["variants"],
        out_fields="GRAFIA,MUNICIPI,ILLA,NOM_GEOGRAFIC,NOM_GEOGRAFIC_ID,PRIORITAT,ESTAT_NG",
        want_geom=False,
    ):
        a = f.get("attributes", {}) or {}
        spelling = a.get("GRAFIA")
        geo_name = a.get("NOM_GEOGRAFIC")
        if not spelling or geo_name is None:
            continue
        rows.append({
            "spelling":           _normalize_article(spelling.strip()),
            "municipality":       _normalize_article(a.get("MUNICIPI")),
            "island_code":        a.get("ILLA"),
            "geographic_name":    geo_name,
            "geographic_name_id": a.get("NOM_GEOGRAFIC_ID"),
            "priority":           a.get("PRIORITAT"),
            "status_code":        a.get("ESTAT_NG"),
        })
    return rows


def write_parquet(rows: list[dict], path: Path):
    if not rows:
        print(f"  ! no rows, skipping {path}", file=sys.stderr)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    cols = list(rows[0].keys())
    def _sqlty(v):
        if isinstance(v, bool): return "BOOLEAN"
        if isinstance(v, int): return "BIGINT"
        if isinstance(v, float): return "DOUBLE"
        return "VARCHAR"
    coltypes = {}
    for c in cols:
        ty = None
        for r in rows:
            v = r.get(c)
            if v is None: continue
            ty = _sqlty(v)
            break
        coltypes[c] = ty or "VARCHAR"
    cols_sql = ", ".join(f'"{c}" {coltypes[c]}' for c in cols)
    con.execute(f'CREATE TABLE t ({cols_sql})')
    placeholders = ", ".join("?" * len(cols))
    con.executemany(
        f'INSERT INTO t VALUES ({placeholders})',
        [[r.get(c) for c in cols] for r in rows],
    )
    con.sql(f"COPY (SELECT * FROM t) TO '{path}' (FORMAT PARQUET)")
    sz = path.stat().st_size / 1024
    print(f"  [OK] {path.relative_to(PROJECT)}  ({sz:.1f} KB, {len(rows):,} rows)", file=sys.stderr)


def check():
    for name in ("ngib.parquet", "ngib_variants.parquet",
                 "ngib_local_types.parquet", "ngib_sources.parquet"):
        p = OUTDIR / name
        if not p.exists():
            print(f"  MISSING: {p.relative_to(PROJECT)}")
            continue
        con = duckdb.connect(":memory:")
        n = con.sql(f"SELECT count(*) FROM read_parquet('{p}')").fetchone()[0]
        sz = p.stat().st_size / 1024
        print(f"  {p.relative_to(PROJECT)}  ({sz:.1f} KB, {n:,} rows)")
    main = OUTDIR / "ngib.parquet"
    if main.exists():
        print("\nBy island:")
        con = duckdb.connect(":memory:")
        for isl, n in con.sql(
            f"SELECT island, count(*) FROM read_parquet('{main}') "
            f"GROUP BY island ORDER BY count(*) DESC"
        ).fetchall():
            print(f"  {isl or '(unknown)':<14} {n:>6,}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="just print stats for existing parquet files")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing files")
    args = ap.parse_args()

    OUTDIR.mkdir(parents=True, exist_ok=True)
    if args.check:
        check()
        return

    main_pq    = OUTDIR / "ngib.parquet"
    var_pq     = OUTDIR / "ngib_variants.parquet"
    lt_pq      = OUTDIR / "ngib_local_types.parquet"
    src_pq     = OUTDIR / "ngib_sources.parquet"

    if args.force or not lt_pq.exists() or not src_pq.exists():
        print("Fetching catalogs (local_types, sources)...", file=sys.stderr)
        lt_rows = fetch_catalog("local_types",
                                "TIPUS_LOCAL_ID,TIPUS_LOCAL,DEFINICIO,TIPUS_INSPIRE_ID")
        src_rows = fetch_catalog("sources", "FONT_ID,FONT,ORGANISME")
        print(f"  local_types: {len(lt_rows)}, sources: {len(src_rows)}", file=sys.stderr)
        write_parquet([{
            "local_type_id":   r.get("TIPUS_LOCAL_ID"),
            "local_type_name": r.get("TIPUS_LOCAL"),
            "definition":      r.get("DEFINICIO"),
            "inspire_type_id": r.get("TIPUS_INSPIRE_ID"),
        } for r in lt_rows], lt_pq)
        write_parquet([{
            "source_id":   r.get("FONT_ID"),
            "source_name": r.get("FONT"),
            "organisme":   r.get("ORGANISME"),
        } for r in src_rows], src_pq)
    else:
        print(f"  [skip] catalogs already present", file=sys.stderr)
        con = duckdb.connect(":memory:")
        lt_rows = con.sql(f"SELECT * FROM read_parquet('{lt_pq}')").fetchall()
        lt_cols = [d[0] for d in con.description]
        lt_rows = [dict(zip(lt_cols, r)) for r in lt_rows]
        for r in lt_rows:
            r["TIPUS_LOCAL_ID"] = r.pop("local_type_id", None)
            r["TIPUS_LOCAL"] = r.pop("local_type_name", None)
            r["DEFINICIO"] = r.pop("definition", None)
        src_rows = con.sql(f"SELECT * FROM read_parquet('{src_pq}')").fetchall()
        src_cols = [d[0] for d in con.description]
        src_rows = [dict(zip(src_cols, r)) for r in src_rows]
        for r in src_rows:
            r["FONT_ID"] = r.pop("source_id", None)
            r["FONT"] = r.pop("source_name", None)
            r["ORGANISME"] = r.pop("organisme", None)

    lt_map = {r.get("TIPUS_LOCAL_ID"): (r.get("TIPUS_LOCAL"), r.get("DEFINICIO"))
              for r in lt_rows if r.get("TIPUS_LOCAL_ID") is not None}
    src_map = {r.get("FONT_ID"): (r.get("FONT"), r.get("ORGANISME"))
               for r in src_rows if r.get("FONT_ID") is not None}

    if args.force or not main_pq.exists():
        print("\nFetching NGIB main toponyms (Layer 0)...", file=sys.stderr)
        rows = fetch_main(lt_map, src_map)
        print(f"  total: {len(rows):,}", file=sys.stderr)
        write_parquet(rows, main_pq)
    else:
        print(f"  [skip] {main_pq.relative_to(PROJECT)} already present", file=sys.stderr)

    if args.force or not var_pq.exists():
        print("\nFetching NGIB variants (Layer 2)...", file=sys.stderr)
        rows = fetch_variants()
        print(f"  total: {len(rows):,}", file=sys.stderr)
        write_parquet(rows, var_pq)
    else:
        print(f"  [skip] {var_pq.relative_to(PROJECT)} already present", file=sys.stderr)

    print("\nDone.", file=sys.stderr)
    check()


if __name__ == "__main__":
    main()
