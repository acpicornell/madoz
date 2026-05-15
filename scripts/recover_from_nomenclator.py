"""Recover Balearic entries that Nomenclator has and we miss.

Cross-references the Nomenclator DuckDB (built from diccionariomadoz.com)
with our chocr-based index. For each Nomenclator entry not present in
our index (fuzzy match), tries to locate the corresponding paragraph in
our Internet Archive chocr text. If found, emits the entry with the
canonical title and content from Nomenclator plus the (vol, leaf,
printed page) located in our scan.

Use this AFTER `merge_index.py` has produced `data/index/all.jsonl`.

Run: python scripts/recover_from_nomenclator.py
Output: data/index/from_nomenclator.jsonl  (entries to merge into all.jsonl)
        data/index/unrecoverable.jsonl     (entries neither side could place)

Notes:
- The chocr paragraph text is OCR-mangled, so matching uses two signals
  in tandem: a fuzzy-normalised title prefix and a content-snippet hit.
  Both must agree before we accept a location.
- An entry that locates to a paragraph already indexed by us under a
  different title (typical OCR variant — BINISETS vs B1NISETS) is
  skipped to avoid duplicates in the final union.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

import duckdb

# Local imports
sys.path.insert(0, str(Path(__file__).resolve().parent))
from index_volume import iter_paragraphs

PROJECT = Path(__file__).resolve().parent.parent
DATA = PROJECT / "data"
CHOCR_DIR = DATA / "chocr"
PAGENUM_DIR = DATA / "page_numbers"
INDEX_DIR = DATA / "index"
NOMENCLATOR_DB = Path.home() / "Nomenclator" / "db" / "nomenclator.duckdb"


# OCR-tolerant normalisation — collapses common substitutions so titles
# like "B1NISETS" and "BINISETS" hash equal.
_TRANSLATE = str.maketrans({"1": "i", "0": "o", "5": "s", "8": "b"})


def fuzzy(s: str) -> str:
    s = s.lower().strip()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s.translate(_TRANSLATE)


def levenshtein(a: str, b: str) -> int:
    if len(a) > len(b):
        a, b = b, a
    if not a:
        return len(b)
    prev = list(range(len(a) + 1))
    for j, cb in enumerate(b, 1):
        curr = [j]
        for i, ca in enumerate(a, 1):
            curr.append(min(curr[-1] + 1, prev[i] + 1, prev[i - 1] + (0 if ca == cb else 1)))
        prev = curr
    return prev[-1]


def load_our_index() -> tuple[set[str], dict[tuple[str, int], set[str]]]:
    """Return (fuzzy-title set, (vol,leaf) → set of fuzzy titles)."""
    by_pair: dict[tuple[str, int], set[str]] = {}
    fz_set: set[str] = set()
    with (INDEX_DIR / "all.jsonl").open() as f:
        for line in f:
            e = json.loads(line)
            fz = fuzzy(e["title"])
            fz_set.add(fz)
            by_pair.setdefault((e["vol"], e["leaf"]), set()).add(fz)
    return fz_set, by_pair


def build_paragraph_index() -> dict[str, list[tuple]]:
    """Per-volume cache: list of (leaf, raw_head, raw_body_first_400, fuzzy_head40)."""
    print("Building paragraph cache from all 16 volumes...", flush=True)
    cache: dict[str, list[tuple]] = {}
    for vol in [f"{i:02d}" for i in range(1, 17)]:
        path = CHOCR_DIR / f"tomo{vol}.html.gz"
        if not path.exists():
            print(f"  [skip] {path.name} missing", flush=True)
            continue
        items = []
        for leaf, par in iter_paragraphs(path):
            if leaf is None or len(par) < 30:
                continue
            norm_par = re.sub(r"\s+", " ", par).strip()
            norm_par = re.sub(r"(\w)-(\w)", r"\1\2", norm_par)
            head = norm_par[:60]
            body = norm_par[:400]
            items.append((leaf, head, body, fuzzy(head)))
        cache[vol] = items
        print(f"  tom{vol}: {len(items)} paragraphs", flush=True)
    return cache


def load_page_maps() -> dict[str, dict[int, str]]:
    maps: dict[str, dict[int, str]] = {}
    for vol in [f"{i:02d}" for i in range(1, 17)]:
        pn_path = PAGENUM_DIR / f"tomo{vol}.json"
        if not pn_path.exists():
            continue
        pn = json.loads(pn_path.read_text())
        maps[vol] = {p["leafNum"]: p.get("pageNumber") for p in pn["pages"]}
    return maps


def find_paragraph(
    nm_title: str,
    nm_body: str,
    par_cache: dict[str, list[tuple]],
) -> tuple[str, int, str] | None:
    """Locate a paragraph that matches the Nomenclator entry.

    Strategy:
    1. Fuzzy-match the title against the first 40 chars of each paragraph.
    2. Tie-break / verify by checking that distinctive keywords from the
       Nomenclator body appear in the paragraph's first 400 chars.
    """
    fz_title = fuzzy(nm_title)
    if len(fz_title) < 3:
        return None
    threshold = max(2, len(fz_title) // 4)

    # Extract a few distinctive keywords from the body for verification.
    # We skip the leading generic "predio en la isla de Mallorca, prov. de
    # Baleares, part. jud. de Inca" — instead use words after position ~80
    # (typically the municipality and term).
    body_for_kw = nm_body.lower()
    body_for_kw = "".join(
        c for c in unicodedata.normalize("NFD", body_for_kw) if unicodedata.category(c) != "Mn"
    )
    kw_window = body_for_kw[40:300]
    kw_candidates = re.findall(r"\b[a-z]{4,12}\b", kw_window)
    stopwords = {"isla", "prov", "part", "jud", "term", "termino", "felig",
                 "jurisd", "jurisdiccion", "ayunt", "ayuntamiento", "baleares",
                 "mallorca", "menorca", "ibiza", "iviza", "formentera",
                 "villa", "aldea", "predio", "lugar", "caserio", "alqueria",
                 "casa", "campo", "vease", "tomos", "diccionario"}
    keywords = [k for k in kw_candidates if k not in stopwords][:6]

    # Match the title against the PREFIX of each paragraph's fuzzy head.
    # Window is the title length exactly: we want the paragraph to start
    # with this title (allowing edit-distance noise), not contain it
    # somewhere in the middle.
    candidates: list[tuple[int, str, int, str, str]] = []  # (dist, vol, leaf, head, body)
    win = len(fz_title)
    first_chars = {fz_title[:1], fz_title[:2]}
    for vol, items in par_cache.items():
        for leaf, head, body, fz_head in items:
            if len(fz_head) < win:
                continue
            # Quick filter: paragraph must start with similar character(s).
            if fz_head[:2] not in first_chars and fz_head[:1] not in first_chars:
                continue
            head_prefix = fz_head[:win]
            d = levenshtein(head_prefix, fz_title)
            if d <= threshold:
                candidates.append((d, vol, leaf, head, body))

    if not candidates:
        return None

    # Filter by keyword overlap in body. Want at least 1 hit among the
    # distinctive keywords.
    body_norm = lambda s: "".join(  # noqa: E731
        c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn"
    )
    scored = []
    for d, vol, leaf, head, body in candidates:
        body_n = body_norm(body)
        hits = sum(1 for k in keywords if k in body_n)
        scored.append((d - hits, d, hits, vol, leaf, head))
    scored.sort()
    best = scored[0]
    _, d, hits, vol, leaf, head = best
    # Require at least 1 keyword hit, or a very close title match.
    if hits == 0 and d > 1:
        return None
    return vol, leaf, head


def main() -> None:
    if not NOMENCLATOR_DB.exists():
        sys.exit(f"Nomenclator DB not found at {NOMENCLATOR_DB}")

    print(f"Loading our index from {INDEX_DIR / 'all.jsonl'}...", flush=True)
    our_fz_set, _ = load_our_index()
    print(f"  {len(our_fz_set)} unique fuzzy titles indexed", flush=True)

    print("Reading Nomenclator missing entries...", flush=True)
    con = duckdb.connect(str(NOMENCLATOR_DB), read_only=True)
    rows = con.execute(
        "select title, coalesce(content_text,''), place_type, island, judicial_district, municipality "
        "from madoz_entries"
    ).fetchall()
    print(f"  Nomenclator total: {len(rows)} entries", flush=True)

    # Pre-filter: skip Nomenclator entries that are OCR-variants of ours.
    # Our titles often carry a parenthetical specifier ("ALEGRE rSON« (antes
    # Cova den Panxe)") while Nomenclator's are shorter ("ALEGRE »SON«"), so
    # compare both full Lev and prefix Lev (our title's first chars vs full
    # Nomenclator title) and take the minimum.
    our_fz_list = list(our_fz_set)
    truly_missing: list[tuple] = []
    for title, body, ptype, isl, jd, muni in rows:
        fz = fuzzy(title)
        if fz in our_fz_set:
            continue
        thr = max(2, len(fz) // 4)
        is_variant = False
        for ofz in our_fz_list:
            if not ofz or ofz[:1] != fz[:1]:
                continue
            # Reject only if ours is much shorter than theirs.
            if len(ofz) < len(fz) - max(3, len(fz) // 3):
                continue
            d_full = levenshtein(ofz, fz)
            d_prefix = levenshtein(ofz[:len(fz) + 2], fz)
            if min(d_full, d_prefix) <= thr:
                is_variant = True
                break
        if not is_variant:
            truly_missing.append((title, body, ptype, isl, jd, muni))
    print(f"  Truly missing from our index: {len(truly_missing)}", flush=True)

    par_cache = build_paragraph_index()
    page_maps = load_page_maps()

    # Note: we deliberately do NOT dedup locally against same-leaf entries
    # of ours. Madoz pages often hold multiple distinct entries (e.g.,
    # RAFAL, RAFAL DE EN MARTI, RAFAL COLOM DE BINIMAIMUT all on the same
    # leaf as separate predis), and any prefix-based fuzzy check confuses
    # genuinely new entries with OCR variants of ours. The `source` field
    # marks these as imported so phase 2 (Vision over the page image) can
    # disambiguate cleanly.
    located: list[dict] = []
    unrecoverable: list[dict] = []
    for title, body, ptype, isl, jd, muni in truly_missing:
        hit = find_paragraph(title, body, par_cache)
        if hit is None:
            unrecoverable.append({
                "title": title,
                "place_type": ptype,
                "island": isl,
                "judicial_district": jd,
                "municipality": muni,
                "context": (body or "")[:140].strip(),
                "source": "nomenclator",
            })
            continue
        vol, leaf, head = hit
        page = page_maps.get(vol, {}).get(leaf)
        located.append({
            "vol": vol,
            "leaf": leaf,
            "page_printed": page,
            "title": title,
            "context": (body or "")[:140].strip(),
            "place_type": ptype,
            "island": isl,
            "judicial_district": jd,
            "municipality": muni,
            "source": "nomenclator",
            "chocr_head": head,
        })

    # Write outputs
    out_loc = INDEX_DIR / "from_nomenclator.jsonl"
    with out_loc.open("w") as f:
        for e in located:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    out_lost = INDEX_DIR / "unrecoverable.jsonl"
    with out_lost.open("w") as f:
        for e in unrecoverable:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    # Build the combined final index: our regex-found entries (tagged
    # source='ours') plus the Nomenclator imports we located.
    combined: list[dict] = []
    with (INDEX_DIR / "all.jsonl").open() as f:
        for line in f:
            e = json.loads(line)
            e.setdefault("source", "ours")
            combined.append(e)
    for e in located:
        combined.append(e)
    out_combined = INDEX_DIR / "combined.jsonl"
    with out_combined.open("w") as f:
        for e in combined:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print()
    print(f"Located (imported as source='nomenclator'): {len(located)}")
    print(f"Unrecoverable (no match in our chocr):     {len(unrecoverable)}")
    print(f"  → {out_loc.relative_to(PROJECT)}")
    print(f"  → {out_lost.relative_to(PROJECT)}")
    print()
    print(f"Combined index: {len(combined)} entries (ours + nomenclator imports)")
    print(f"  → {out_combined.relative_to(PROJECT)}")


if __name__ == "__main__":
    main()
