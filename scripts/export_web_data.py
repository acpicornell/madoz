"""Export text_entries (joined with madoz_entries.url) to a single
web/data.json — consumed directly by the static web (no DuckDB-WASM).

Run after any data refresh:
  python scripts/export_web_data.py
"""
from __future__ import annotations
import json
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
OUT = PROJECT / "web" / "data.json"


def _load_coords() -> dict[tuple[str, int, str, str], dict]:
    """Per-entry coords keyed by (vol, leaf, title, island).

    Produced by ``scripts/enrich_coords.py``. ``island`` is part of the
    key so that two same-leaf same-title homonyms (e.g. CONSELL Mallorca
    and CONSELL Menorca on leaf 06/571) keep separate coordinates — a
    dict keyed only on (vol, leaf, title) silently collapses them and
    causes the wrong-island bug observed on the live map.

    Returns an empty dict if the file is missing so export still works
    before the NGIB pipeline has been run.
    """
    coords_path = PROJECT / "data" / "coords.json"
    if not coords_path.exists():
        return {}
    payload = json.loads(coords_path.read_text())
    by_key: dict[tuple[str, int, str, str], dict] = {}
    for c in payload:
        key = (c["vol"], int(c["leaf"]), c["title"], c.get("island") or "")
        by_key[key] = c
    return by_key


def main() -> None:
    con = duckdb.connect(str(DB), read_only=True)
    rows = con.execute(
        """
        SELECT t.id, t.vol, t.leaf, t.page_printed, t.title,
               t.place_type, t.island, t.judicial_district, t.municipality,
               t.description, t.stats, t.cross_references, t.confidence,
               t.note, m.url AS madoz_url, m.title AS madoz_title,
               m.content_text AS madoz_content
        FROM text_entries t
        LEFT JOIN madoz_entries m ON m.id = t.madoz_entry_id
        ORDER BY t.title
        """
    ).fetchall()
    cols = [
        "id", "vol", "leaf", "page_printed", "title",
        "place_type", "island", "judicial_district", "municipality",
        "description", "stats", "cross_references", "confidence",
        "note", "madoz_url", "madoz_title", "madoz_content",
    ]

    coords_by_key = _load_coords()

    entries = []
    n_with_coords = 0
    for row in rows:
        d = dict(zip(cols, row))
        # NGIB-derived coordinates (added 2026-05-22). Only inline the
        # numeric fields the map needs — the full match metadata is
        # available in data/coords.json if anyone wants the audit.
        c = coords_by_key.get(
            (d["vol"], d["leaf"], d["title"], d.get("island") or "")
        )
        if c and "lon" in c and "lat" in c:
            d["lon"] = c["lon"]
            d["lat"] = c["lat"]
            if c.get("fallback"):
                d["coord_fallback"] = c["fallback"]
            n_with_coords += 1
        # stats arrives as JSON string from DuckDB; parse so frontend
        # doesn't have to re-parse a string per row.
        if isinstance(d["stats"], str) and d["stats"]:
            try:
                d["stats"] = json.loads(d["stats"])
            except json.JSONDecodeError:
                d["stats"] = None
        # cross_references arrives as a list already
        if d["cross_references"] is None:
            d["cross_references"] = []
        # Facsimile link to the Internet Archive scan. Page-level (one
        # leaf can hold several entries), not paragraph-level — the UI
        # makes that clear.
        d["ia_url"] = (
            f"https://archive.org/details/diccionariogeogr{d['vol']}mado"
            f"/page/n{d['leaf']}/mode/2up"
        )
        # Only include the diccionariomadoz.com content when it's
        # significantly longer than our description (i.e. they have
        # content we don't). Saves ~3-5 MB of redundant JSON.
        our_len = len(d.get("description") or "")
        their_len = len(d.get("madoz_content") or "")
        if their_len < max(2 * our_len, 1000):
            d["madoz_content"] = None
        # drop empty/falsy fields to keep the JSON small
        for k in list(d):
            if d[k] in (None, "", []):
                del d[k]
        entries.append(d)

    counts = con.execute(
        "SELECT COUNT(*) FROM madoz_entries"
    ).fetchone()[0]

    payload = {
        "generated_with": "scripts/export_web_data.py",
        "text_total": len(entries),
        "madoz_total": counts,
        "entries": entries,
    }

    OUT.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Wrote {OUT.relative_to(PROJECT)}  ({OUT.stat().st_size/1024:.1f} KB, "
          f"{len(entries)} entries, {n_with_coords} with coords)")


if __name__ == "__main__":
    main()
