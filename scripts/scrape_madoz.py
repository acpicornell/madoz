"""Scrape the Madoz dictionary (1845-1850), Balearic entries.

Source: diccionariomadoz.com, a WordPress site that re-publishes Pascual
Madoz's "Diccionario geográfico-estadístico-histórico de España y sus
posesiones de ultramar" article by article. The Balearic category (id=7)
has ~1,150 entries.

We use the public WP REST API instead of scraping HTML:
    /wp-json/wp/v2/posts?categories=7&per_page=100&page=N
    /wp-json/wp/v2/tags?include=...

Raw responses are stored verbatim as JSONL under data/madoz/ so we can
re-parse without hitting the server again. The loader fills the tables
defined in db/schema.sql (madoz_entries, madoz_tags, madoz_entry_tags).

Politeness: low concurrency, 1.5 s between requests, exponential backoff
on 429/5xx, neutral User-Agent. We do not send any header that could
identify the operator.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Iterator

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
SCHEMA = PROJECT / "db" / "schema.sql"
RAW_DIR = PROJECT / "data" / "madoz"
POSTS_JSONL = RAW_DIR / "posts.jsonl"
TAGS_JSONL = RAW_DIR / "tags.jsonl"
# Additional posts off the normal flow: entries from the source site
# that are mis-categorized (not under `categories=7` Baleares) and that
# scrape_madoz_extras.py recovers by slug. They are merged into
# posts.jsonl both in fresh and --from-cache modes. A unique id
# guarantees no duplication.
EXTRAS_JSONL = RAW_DIR / "extras.jsonl"

API = "https://www.diccionariomadoz.com/wp-json/wp/v2"
CATEGORY_BALEARES = 7
PER_PAGE = 100
SLEEP_BETWEEN = 1.5   # seconds between successful requests
MAX_RETRIES = 5

# Plain Safari UA. No "research", no e-mail, no project name.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15")

# Heuristic parsers ---------------------------------------------------------

ISLANDS = ("Mallorca", "Menorca", "Ibiza", "Iviza", "lbiza",
           "Formentera", "Cabrera")
ISLAND_TITLES = {"MALLORCA": "Mallorca", "MENORCA": "Menorca",
                 "IBIZA": "Ibiza", "IVIZA": "Ibiza", "LBIZA": "Ibiza",
                 "FORMENTERA": "Formentera", "CABRERA": "Cabrera"}

# Maps the lead-in token (or first token after a "porción de…" preamble)
# to its canonical Spanish lemma. Madoz uses heavy abbreviation: 'alq.',
# 'v.', 'l.', 'cas.', 'cast.' etc. The lookup key has the trailing dot
# already removed. Admin units are in a separate dict so the fallback
# search can prefer physical features over 'provincia de Baleares',
# which appears in nearly every article.
PLACE_TYPE_LEMMA: dict[str, str] = {
    # Abbreviations as printed.
    "alq": "alquería", "v": "villa", "l": "lugar", "ald": "aldea",
    "cas": "caserío", "cast": "castillo", "c": "ciudad",
    # Full forms — physical and built features.
    "predio": "predio", "estancia": "estancia",
    "ciudad": "ciudad", "villa": "villa", "lugar": "lugar",
    "aldea": "aldea", "caserío": "caserío", "caserio": "caserío",
    "alquería": "alquería", "alqueria": "alquería",
    "castillo": "castillo", "torre": "torre",
    "casa": "casa", "granja": "granja", "cortijo": "cortijo",
    "isla": "isla", "islote": "islote", "islita": "islote",
    "isleta": "islote",
    "monte": "monte", "montaña": "montaña", "sierra": "sierra",
    "valle": "valle",
    "cabo": "cabo", "punta": "punta", "puerto": "puerto",
    "ensenada": "ensenada", "cala": "cala", "caleta": "cala",
    "bahía": "bahía", "bahia": "bahía", "playa": "playa",
    "fuente": "fuente", "lago": "lago", "laguna": "laguna",
    "cueva": "cueva",
    "ermita": "ermita", "santuario": "santuario",
    "convento": "convento", "monasterio": "monasterio",
    "iglesia": "iglesia", "barrio": "barrio", "arrabal": "arrabal",
    "feligresia": "feligresía", "feligresía": "feligresía",
    "parr": "parroquia", "parroquia": "parroquia",
    "rio": "río", "río": "río", "torrente": "torrente",
    "corriente": "torrente",
}
PLACE_TYPE_ADMIN: dict[str, str] = {
    "audiencia": "audiencia",
    "diócesis": "diócesis", "diocesis": "diócesis",
    "provincia": "provincia",
    "partido": "partido judicial",
    "partido judicial": "partido judicial",
}
PLACE_TYPE_MULTI = ("partido judicial",)

TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    return html.unescape(TAG_RE.sub(" ", s)).strip()


def collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def detect_place_type(text: str) -> str | None:
    """Best-effort entry-type extractor.

    Strategy:
      1. Multi-word phrase at start ("partido judicial …").
      2. Single token at the very start, with the trailing dot stripped
         (Madoz prints 'alq.', 'v.', 'l.', etc.). Tries the physical
         vocabulary first, then admin.
      3. First known lemma found anywhere in the first 120 chars, again
         physical before admin — covers "porción de playa …", "pequeña
         isla del Mediterráneo …" without falling back to 'provincia',
         which appears in almost every article as "provincia de Baleares".
    Longer lemmas win within a tier so 'caserío' beats 'cas'.
    """
    head = text[:120]
    low = head.lower()
    for phrase in PLACE_TYPE_MULTI:
        if low.startswith(phrase):
            return PLACE_TYPE_ADMIN[phrase]
    m = re.match(r"\s*([a-záéíóúñ]+)\.?", head, flags=re.IGNORECASE)
    tok = m.group(1).lower() if m else None
    if tok:
        if tok in PLACE_TYPE_LEMMA:
            return PLACE_TYPE_LEMMA[tok]
        if tok in PLACE_TYPE_ADMIN:
            return PLACE_TYPE_ADMIN[tok]
    # Fallback: scan the first clause but skip locator hits. In Madoz
    # the entry type is the head noun of clause 1 ("dos predios en la
    # isla de Mallorca"); 'isla' here is the locator, not the type.
    clause1 = re.split(r"[,;:]", low, maxsplit=1)[0][:80]
    locator_pre = re.compile(
        r"\b(?:en|de|á|a)\s+(?:la|el|lo|los|las)\s*$|\bdel\s*$",
        flags=re.IGNORECASE)
    def in_locator(at: int) -> bool:
        return bool(locator_pre.search(clause1[max(0, at - 25):at]))
    for tier in (PLACE_TYPE_LEMMA, PLACE_TYPE_ADMIN):
        for lemma in sorted((k for k in tier if k not in PLACE_TYPE_MULTI),
                            key=len, reverse=True):
            for mm in re.finditer(rf"\b{re.escape(lemma)}s?\b", clause1):
                if not in_locator(mm.start()):
                    return tier[lemma]
    return None


def detect_island(text: str, title: str | None = None) -> str | None:
    """Resolve which Balearic island the entry refers to.

    Order of evidence, strongest first:
      1. The title is literally an island name (MALLORCA, MENORCA, …).
         Madoz dedicates one article per island, so this is unambiguous.
      2. A phrase of the form 'isla … de X' where … may contain up to
         ~40 chars of intervening words ('isla y diócesis de Menorca',
         'isla y partido jud. de Ibiza', …). Avoids the false positive
         where the text mentions Mallorca only as the maritime province.
      3. Bare mention of an island name anywhere in the text (last
         resort; only used when 1 and 2 fail).
    """
    if title:
        key = re.sub(r"\(.*?\)", "", title).strip().upper()
        if key in ISLAND_TITLES:
            return ISLAND_TITLES[key]
    for isl in ISLANDS:
        if re.search(rf"\bisla\b[^.,]{{0,40}}\bde\s+{isl}\b", text, flags=re.IGNORECASE):
            return "Ibiza" if isl in ("Iviza", "lbiza") else isl
    for isl in ISLANDS:
        if re.search(rf"\b{isl}\b", text):
            return "Ibiza" if isl in ("Iviza", "lbiza") else isl
    return None


# Canonical judicial districts in Balears (~1845). Keys are lowercased
# OCR variants of the captured fragment; values are the canonical name.
# Madoz scans produce many spellings ("Mauacor", "I n c", "Mahou", "Pal",
# "Cindadela"…) that all collapse to six districts.
JUDICIAL_CANONICAL = ("Inca", "Manacor", "Palma", "Mahón", "Ciudadela", "Ibiza")
JUDICIAL_VARIANTS: dict[str, str] = {
    # Inca
    "inca": "Inca", "i n c": "Inca", "i n c a": "Inca",
    "juca": "Inca", "idcli": "Inca", "idcîi": "Inca",
    # Manacor
    "manacor": "Manacor", "mauacor": "Manacor", "menacor": "Manacor",
    "monacor": "Manacor", "mnnacor": "Manacor", "manacur": "Manacor",
    # Palma
    "palma": "Palma", "pal": "Palma", "p a l m": "Palma",
    "p a l m a": "Palma",
    # Mahón (we keep the accent as the canonical Spanish form Madoz uses)
    "mahon": "Mahón", "mahón": "Mahón", "mahou": "Mahón",
    # Ciudadela
    "ciudadela": "Ciudadela", "ciudadeia": "Ciudadela",
    "cindadela": "Ciudadela", "ciudadcla": "Ciudadela",
    # Ibiza (only one district on the island in 1845)
    "ibiza": "Ibiza", "iviza": "Ibiza", "lbiza": "Ibiza",
}


def normalize_judicial(raw: str | None) -> str | None:
    """Map an OCR-captured fragment to a canonical judicial district name."""
    if not raw:
        return None
    s = collapse_ws(raw).strip(" .,;:").lower()
    # Strip common trailing junk picked up by greedy matches.
    s = re.sub(r"\s+(s\s*i\s*t|sit)\b.*$", "", s)
    if s in JUDICIAL_VARIANTS:
        return JUDICIAL_VARIANTS[s]
    # Substring fallback: handles "inca sit", "palma s i t", "manacor sit",
    # plus rare cases where the capture group includes the next word.
    for key, canon in JUDICIAL_VARIANTS.items():
        if re.search(rf"\b{re.escape(key)}\b", s):
            return canon
    return None


def detect_judicial_district(text: str) -> str | None:
    """Extract the judicial district (partido judicial) from a Madoz entry.

    Madoz's standard locution is "partido jud. de X" but the OCR pass on
    diccionariomadoz.com produces many variants: "partido Jud.",
    "partidoJud.", "partido j u d .", "partidojudicial", "P. J. de X",
    plus capital-D in the joining "De". We match case-insensitively and
    then normalize the captured fragment.
    """
    # Stop at punctuation, the next Madoz section marker (SIT.), or any
    # connecting word that would otherwise be slurped into the capture.
    # Note: we omit bare "a" (only keep "á") because the OCR sometimes
    # prints place names letter-spaced ("P a l m a") and bare "a" would
    # stop the capture after the first letter. normalize_judicial()
    # rescues the value either way via substring lookup.
    stop = (r"(?=[,.;:]|\s+(?:á|de|y|que|en|al|del|cuyo|donde|"
            r"t[ée]rmino|jurisd|distante|adm|prov|di[oó]c|"
            r"s\s*i\s*t|sit)\b)")
    pats = (
        # "partido jud. de X", "partido judicial de X", "partido j u d . de X",
        # "partidojudicial de X" (no space). Between the letters and the
        # joining "de", the OCR may insert any combination of spaces and
        # dots, hence the loose [\s.]* glue.
        rf"partido\s*j[\s.]*u[\s.]*d[\s.]*(?:icial)?[\s.]*"
        rf"(?:y\s+\w+\.?\s+de\s+(?:rent\.?\s+de\s+)?|de\s+)?"
        rf"([\wáéíóúñ\- ]+?){stop}",
        # "P. J. de X"
        rf"\bp\.\s*j\.\s+(?:de\s+)?([\wáéíóúñ\- ]+?){stop}",
        # "part. jud. de X" (short form)
        rf"part\.\s*jud(?:icial)?\.?\s+(?:de\s+)?([\wáéíóúñ\- ]+?){stop}",
    )
    for p in pats:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return normalize_judicial(m.group(1))
    return None


def detect_municipality(text: str) -> str | None:
    # "término y jurisd. de la v. de Manacor", with many variants.
    m = re.search(
        r"(?:t[ée]rmino(?:\s+y\s+jurisd(?:icci[oó]n)?\.?)?|jurisd(?:icci[oó]n)?\.?)"
        r"\s+(?:de\s+la\s+(?:v\.|villa|c\.|ciudad|l\.|lugar)\s+de\s+|de\s+)"
        r"([A-Z][\wáéíóúñ’'\- ]+?)(?=[,.;]|\s+es\b|\s+se\b)",
        text,
    )
    return collapse_ws(m.group(1).strip(" .")) if m else None


def parse_post(post: dict) -> tuple:
    """Map a WP post JSON to a row tuple for madoz_entries."""
    content_html = (post.get("content") or {}).get("rendered") or ""
    content_text = collapse_ws(strip_html(content_html))
    title = strip_html((post.get("title") or {}).get("rendered") or "")
    return (
        int(post["id"]),
        post.get("slug") or "",
        title,
        post.get("link") or "",
        post.get("date") or None,
        post.get("modified") or None,
        content_html,
        content_text,
        len(content_text),
        detect_place_type(content_text),
        detect_island(content_text, title),
        detect_judicial_district(content_text),
        detect_municipality(content_text),
    )


# HTTP layer ----------------------------------------------------------------

def http_get(url: str) -> tuple[int, dict, bytes]:
    """GET with neutral headers. Returns (status, headers, body)."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Language": "es,ca;q=0.8,en;q=0.5",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() or b""


def get_json(url: str) -> tuple[dict | list, dict]:
    """GET JSON with retries on 429/5xx. Returns (data, response_headers)."""
    delay = 2.0
    last_err: str | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        status, headers, body = http_get(url)
        if status == 200:
            return json.loads(body.decode("utf-8")), headers
        if status in (429, 500, 502, 503, 504):
            last_err = f"HTTP {status}"
            print(f"  ! {last_err} on {url} (attempt {attempt}/{MAX_RETRIES}), sleep {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
            continue
        # 4xx other than 429: don't retry.
        raise RuntimeError(f"HTTP {status} on {url}: {body[:200]!r}")
    raise RuntimeError(f"gave up after {MAX_RETRIES} retries: {last_err}")


# Fetch loops ---------------------------------------------------------------

def iter_posts(category: int, limit: int | None) -> Iterator[dict]:
    page = 1
    total: int | None = None
    yielded = 0
    while True:
        url = f"{API}/posts?categories={category}&per_page={PER_PAGE}&page={page}&orderby=id&order=asc"
        data, headers = get_json(url)
        if total is None:
            total = int(headers.get("X-WP-Total", "0") or 0) or None
        if not isinstance(data, list) or not data:
            break
        for post in data:
            yield post
            yielded += 1
            if limit is not None and yielded >= limit:
                return
        print(f"  posts page {page}: +{len(data)} (running total {yielded}"
              f"{f'/{total}' if total else ''})")
        if len(data) < PER_PAGE:
            break
        page += 1
        time.sleep(SLEEP_BETWEEN)


def fetch_tags(tag_ids: Iterable[int]) -> list[dict]:
    """Fetch tag metadata for the given ids, in batches of 100."""
    ids = sorted(set(int(t) for t in tag_ids))
    out: list[dict] = []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        url = f"{API}/tags?include={','.join(str(x) for x in chunk)}&per_page=100"
        data, _ = get_json(url)
        if isinstance(data, list):
            out.extend(data)
        print(f"  tags chunk {i // 100 + 1}: +{len(data) if isinstance(data, list) else 0}")
        time.sleep(SLEEP_BETWEEN)
    return out


# DB layer ------------------------------------------------------------------

def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA.read_text())


def load_into_db(con: duckdb.DuckDBPyConnection, posts: list[dict], tags: list[dict]) -> None:
    rows = [parse_post(p) for p in posts]
    con.execute("BEGIN")
    con.execute("DELETE FROM madoz_entry_tags")
    con.execute("DELETE FROM madoz_entries")
    con.execute("DELETE FROM madoz_tags")
    con.executemany(
        """INSERT INTO madoz_entries
           (id, slug, title, url, date_published, date_modified,
            content_html, content_text, content_length,
            place_type, island, judicial_district, municipality)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    con.executemany(
        "INSERT INTO madoz_tags (id, name, slug, count) VALUES (?, ?, ?, ?)",
        [(int(t["id"]), strip_html(t.get("name") or ""), t.get("slug") or "",
          int(t.get("count") or 0)) for t in tags],
    )
    pairs: list[tuple[int, int]] = []
    for p in posts:
        for tid in p.get("tags") or []:
            pairs.append((int(p["id"]), int(tid)))
    if pairs:
        con.executemany(
            "INSERT OR IGNORE INTO madoz_entry_tags (entry_id, tag_id) VALUES (?, ?)",
            pairs,
        )
    con.execute("COMMIT")


# Entry point ---------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N posts (for quick sampling)")
    ap.add_argument("--from-cache", action="store_true",
                    help="skip the network, re-parse the JSONL files in data/madoz/")
    ap.add_argument("--no-db", action="store_true",
                    help="only write JSONL; skip DuckDB loading")
    args = ap.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    DB.parent.mkdir(parents=True, exist_ok=True)
    posts: list[dict] = []
    tags: list[dict] = []

    if args.from_cache:
        if not POSTS_JSONL.exists():
            sys.exit(f"no cache at {POSTS_JSONL}")
        with POSTS_JSONL.open() as f:
            posts = [json.loads(line) for line in f if line.strip()]
        if TAGS_JSONL.exists():
            with TAGS_JSONL.open() as f:
                tags = [json.loads(line) for line in f if line.strip()]
        print(f"Loaded {len(posts)} posts and {len(tags)} tags from cache.")
    else:
        print(f"Fetching Madoz posts (category {CATEGORY_BALEARES})...")
        with POSTS_JSONL.open("w") as f:
            for post in iter_posts(CATEGORY_BALEARES, args.limit):
                f.write(json.dumps(post, ensure_ascii=False) + "\n")
                posts.append(post)
        print(f"Total posts fetched: {len(posts)}")

        tag_ids = {tid for p in posts for tid in (p.get("tags") or [])}
        print(f"\nFetching {len(tag_ids)} tags...")
        tags = fetch_tags(tag_ids)
        with TAGS_JSONL.open("w") as f:
            for t in tags:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")

    if EXTRAS_JSONL.exists():
        seen = {int(p["id"]) for p in posts}
        added = 0
        with EXTRAS_JSONL.open() as f:
            for line in f:
                if not line.strip():
                    continue
                p = json.loads(line)
                if int(p["id"]) not in seen:
                    posts.append(p)
                    seen.add(int(p["id"]))
                    added += 1
        if added:
            print(f"Merged {added} extras from {EXTRAS_JSONL.name} (total posts: {len(posts)})")

    if args.no_db:
        print("--no-db: skipping DuckDB.")
        return

    print(f"\nLoading into {DB}...")
    con = duckdb.connect(str(DB))
    ensure_schema(con)
    load_into_db(con, posts, tags)

    n_e, n_t, n_et = con.execute(
        "SELECT (SELECT COUNT(*) FROM madoz_entries), "
        "       (SELECT COUNT(*) FROM madoz_tags), "
        "       (SELECT COUNT(*) FROM madoz_entry_tags)"
    ).fetchone()
    print(f"madoz_entries: {n_e} | madoz_tags: {n_t} | madoz_entry_tags: {n_et}")

    print("\n--- Distribution by island (parsed) ---")
    for r in con.execute(
        "SELECT COALESCE(island, '(unknown)') AS island, COUNT(*) "
        "FROM madoz_entries GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {r[0]:15} {r[1]:>5}")

    print("\n--- Top place types (parsed) ---")
    for r in con.execute(
        "SELECT COALESCE(place_type, '(unknown)') AS pt, COUNT(*) "
        "FROM madoz_entries GROUP BY 1 ORDER BY 2 DESC LIMIT 12"
    ).fetchall():
        print(f"  {r[0]:20} {r[1]:>5}")

    print("\n--- Sample ---")
    for r in con.execute(
        "SELECT title, place_type, island, judicial_district, municipality "
        "FROM madoz_entries ORDER BY content_length DESC LIMIT 5"
    ).fetchall():
        print(f"  {r[0][:25]:25} | {str(r[1])[:15]:15} | {str(r[2])[:10]:10} "
              f"| pj={str(r[3])[:14]:14} | mun={r[4]}")


if __name__ == "__main__":
    main()
