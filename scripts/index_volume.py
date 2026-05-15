"""Index a Madoz volume by exploiting hOCR paragraph structure.

Phase 1 of the "Madoz done right" pipeline: for each Balearic entry,
record its exact location in the original edition — volume, leaf,
printed page — so phase 2 (Claude Vision extraction) can fetch the
correct page image directly.

Strategy (rewritten to avoid the regex-on-flat-text mess):

1. Stream the chocr and emit one **paragraph** (`<p class="ocr_par">`)
   per leaf at a time. Each paragraph preserves the editorial unit of
   the Madoz: most dictionary entries are a single paragraph.
2. For each paragraph, check whether it starts with a Madoz title
   (uppercase letters followed by `:` or `;`).
3. Verify that the paragraph body mentions a Balearic island with a
   geographic indicator nearby.
4. Resolve `leafNum -> printed page` via `page_numbers.json`.
5. Emit JSON Lines.

Run: python scripts/index_volume.py <vol>   (e.g. 02)
Output: data/index/tomo<vol>.jsonl
"""
from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DATA = PROJECT / "data"
CHOCR_DIR = DATA / "chocr"
PAGENUM_DIR = DATA / "page_numbers"
OUT_DIR = DATA / "index"

# Title patterns run on a single paragraph's text, not on the concatenated
# leaf text. Madoz titles: "ARTA:", "BAÑALBUFAR:", "MARÍA (santa):",
# "ADAYA ó DADAYA:", "VICENCIO Y ESCADAS (San):". A title may carry a
# lowercase parenthetical specifier but always starts with 2+ caps (or
# a cap + digits when the OCR has mangled chars like "B1NIBASI").
#
# STRICT version: requires a separator (`:`, `;`, `-.`, `.-`) between
# the title and body. This matches canonical Madoz and the majority of
# entries.
#
# OCR-noise tolerance: paragraphs are sometimes prefixed by stray glyphs
# (curly quotes, tildes, "t0", apostrophes) and the parenthetical part
# of a title is often mangled (e.g. "AMER Isos)", "BALME (c\n)",
# "ESTARAS ¿son)", "LLANERAS (sój"). To recover those, we accept ANY
# non-separator char inside the title body — the safeguard is the
# initial 2+ caps run and the Balearic filter on the body afterwards.
PAT_ENTRY_STRICT = re.compile(
    r'^[\s\'\"~`«»_,\.\-\(\)\[\]\{\}t0\d]{0,6}'  # leading OCR junk
    r'(?P<title>'
        r'[A-ZÑÁÉÍÓÚÜ][A-ZÑÁÉÍÓÚÜ0-9]{1,}'      # initial caps run (>=2)
        r'[^:;\n]{0,50}?'                       # title body (anything but the separator)
    r')'
    r"\s*(?:[:;]\.?|-\.|\.\-)\s*[\'\"]?\s*"
    r'(?P<body>.+)',
    re.DOTALL,
)

# LOOSE version: separator is optional. Used ONLY when STRICT fails;
# combined with a body-marker safeguard (the body must start with a
# canonical Madoz marker: predio, v., cas., alq., etc.). Recovers
# entries where the OCR has dropped the `:`.
PAT_ENTRY_LOOSE = re.compile(
    r'^\s*'
    r'(?P<title>'
        r'[A-ZÑÁÉÍÓÚÜ][A-ZÑÁÉÍÓÚÜ0-9]{1,}'
        r"(?:[A-ZÑÁÉÍÓÚÜa-zñáéíóú0-9 \-,\.\']"
            r"|\([A-Za-zñáéíóúÑÁÉÍÓÚÜ0-9\s\.,\-\']{1,30}\)"
        r'){0,40}?'
    r')'
    r'\s+'
    r'(?P<body>.+)',
    re.DOTALL,
)

# Canonical body-start markers for a Madoz entry. If the body begins
# with one, the paragraph is almost certainly an entry — letting us
# accept paragraphs where the OCR dropped the `:` separator. Note:
# abbreviations (v., cas., l., ...) require the literal period; full
# words (predio, cala, ...) require a non-letter terminator.
BODY_MARKER = re.compile(
    r'^\(?\s*(?:'
    # Full words — must be followed by a non-letter
    r'(?:predio|predios|alqueria|alquería|aldea|villa|lugar|caserío|caserio|'
    r'casa|cala|cabo|punta|monte|sierra|valle|bahia|bahía|playa|puerto|isla|'
    r'isleta|islote|partido|parroquia|feligresia|feligresía|granja|cortijo|'
    r'barrio|estancia|arroyo|río|rio|torre|hacienda|ribera|despoblado|fuente|'
    r'provincia|pueblo|pago|coto|baron|baronia|baronía|condado|jurisdicci[óo]n|'
    r'departamento|distrito|cuart[óo]n|porci[óo]n|territorio|antigua|antiguo|'
    r'pequeña|pequeño|pequeñas|pequeños|cortijada|caser[ií]a|granja|señoría|'
    r'cab|cabezada|colina|laguna|saliente|punto|piedra|peñ[óo]n|peñas?|nombre'
    r')(?=[^A-Za-zñáéíóúÑÁÉÍÓÚÜ])'
    r'|'
    # Abbreviations — the literal period is the delimiter
    r'(?:v|l|cas|c|ald|alq|parr|felig|r|hac|desp|prov|distr|cot|ant|cap|t|fr)\.'
    r')',
    re.IGNORECASE,
)

PAT_BALEAR = re.compile(
    r'\b(?:isla|isl\.|prov\.|adm\.|dióc\.|partido|cala|punta|cabo|sierra|'
    r'valle|bahia|bahía|monte|playa|puerto)\.?'
    r'.{0,40}'
    r'(?:Mallorca|Menorca|Ibiza|Iviza|Formentera|Cabrera|'
    r'Baleares|Raleares|Paleares)\b',
    re.IGNORECASE | re.DOTALL,
)

# Titles that are not entries (front matter, indices, pure OCR garbage).
TITLE_BLACKLIST = {
    "DICCIONARIO", "GEOGRAFICO", "GEOGRÁFICO", "HISTORICO", "HISTÓRICO",
    "ESPAÑA", "ULTRAMAR", "POSESIONES", "MADRID", "TOMO", "ADVERTENCIA",
    "PROLOGO", "PRÓLOGO", "INTRODUCCION", "INTRODUCCIÓN", "INDICE",
    "ÍNDICE", "MAPAS", "PESOS", "MEDIDAS", "ABREVIATURAS", "FIN",
}

# Page-header / running-title tokens to reject when they appear as titles.
HEADER_TOKENS = re.compile(
    r'^(?:DICCIONARIO|GEOGRÁFICO|HISTÓRICO|ESPAÑA|ULTRAMAR|'
    r'POSESIONES|TOMO|FIN|ADVERTENCIA)\b',
    re.IGNORECASE,
)

PAGE_PAT = re.compile(r'class="ocr_page" id="page_(\d+)"')
PAR_OPEN_PAT = re.compile(r'<p class="ocr_par"')
CHAR_PAT = re.compile(r'<span class="ocrx_cinfo"[^>]*>([^<])</span>')


def iter_paragraphs(chocr_path: Path):
    """Yield (leaf:int, paragraph_text:str) for each paragraph in a volume.

    Streams the file without loading it all into memory. Relies on the
    fact that each hOCR tag is on its own line in the IA chocr file.
    """
    opener = gzip.open if chocr_path.suffix == ".gz" else open
    current_leaf = None
    buf: list[str] = []
    in_par = False
    with opener(chocr_path, "rt", encoding="utf-8") as f:
        for line in f:
            m_page = PAGE_PAT.search(line)
            if m_page:
                if buf:
                    yield current_leaf, "".join(buf)
                    buf = []
                current_leaf = int(m_page.group(1))
                in_par = False
                continue
            if PAR_OPEN_PAT.search(line):
                if buf:
                    yield current_leaf, "".join(buf)
                    buf = []
                in_par = True
                continue
            if "</p>" in line:
                if buf:
                    yield current_leaf, "".join(buf)
                    buf = []
                in_par = False
                continue
            if in_par:
                for cm in CHAR_PAT.finditer(line):
                    buf.append(cm.group(1))
    if buf:
        yield current_leaf, "".join(buf)


def index_volume(vol: str) -> list[dict]:
    chocr_path = CHOCR_DIR / f"tomo{vol}.html.gz"
    pn_path = PAGENUM_DIR / f"tomo{vol}.json"
    if not chocr_path.exists():
        sys.exit(f"Missing {chocr_path}. Run first: python scripts/fetch_volume.py {vol}")
    if not pn_path.exists():
        sys.exit(f"Missing {pn_path}. Run first: python scripts/fetch_volume.py {vol}")

    pn = json.loads(pn_path.read_text())
    leaf2page = {p["leafNum"]: p.get("pageNumber") for p in pn["pages"]}

    entries: list[dict] = []
    n_par = 0
    for leaf, par_text in iter_paragraphs(chocr_path):
        n_par += 1
        if leaf is None or not par_text:
            continue
        # Collapse whitespace; keep all content otherwise.
        norm = re.sub(r"\s+", " ", par_text).strip()
        # Rejoin words split by end-of-line hyphenation: "Mallor-ca" ->
        # "Mallorca", "Ba-leares" -> "Baleares". Crucial for the Balearic
        # filter, which otherwise misses entries where the island name
        # lands right on a column break.
        norm = re.sub(r"(\w)-(\w)", r"\1\2", norm)
        if len(norm) < 20:
            continue  # paragraph too short to be a real entry
        # Try strict separator first (matches most canonical entries).
        # If it fails, fall back to the loose pattern with a body-marker
        # safeguard.
        m = PAT_ENTRY_STRICT.match(norm)
        require_body_marker = False
        if not m:
            m = PAT_ENTRY_LOOSE.match(norm)
            require_body_marker = True
        if not m:
            continue
        title = m.group("title").strip(" .,;:-")
        body = m.group("body").strip()
        # Title quality filters
        if not (3 <= len(title) <= 60):
            continue
        # We allow digits in the title for OCR fixes (B1NIBASI), but
        # require at least 3 actual letters to reject things like "B1"
        # or roman numerals ("II", "III", "IV").
        n_letters = sum(1 for c in title if c.isalpha())
        if n_letters < 3:
            continue
        if HEADER_TOKENS.match(title):
            continue
        if title.upper() in TITLE_BLACKLIST:
            continue
        # Skip titles that are just a sequence of single letters ("S. C.").
        if re.fullmatch(r"(?:[A-ZÑÁÉÍÓÚÜ]\.?\s*){1,3}", title):
            continue
        # Loose-version safeguard: with no separator, the body must
        # begin with a canonical Madoz marker.
        if require_body_marker and not BODY_MARKER.match(body):
            continue
        # Balearic filter: the OCR sometimes glues articles to the
        # following word ("laisla", "delas") and `\b` then fails. Insert
        # spaces before the search to recover entries where the island
        # mention has a particle stuck to it.
        body_search = re.sub(
            r'\b(la|el|las|los|en|de|del|y)(isla|isl|prov|partido|cala|punta|'
            r'cabo|sierra|valle|bahia|bahía|monte|playa|puerto|tercio|distrito|'
            r'distr|dióc|adm)',
            r'\1 \2', body, flags=re.IGNORECASE)
        if not PAT_BALEAR.search(body_search):
            continue
        entries.append({
            "vol": vol,
            "leaf": leaf,
            "page_printed": leaf2page.get(leaf),
            "title": title,
            "context": body[:140].strip(),
        })

    # Deduplicate by (vol, leaf, title_norm)
    seen: set[tuple] = set()
    unique: list[dict] = []
    for e in entries:
        key = (e["leaf"], e["title"].upper())
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    print(f"  {n_par} paragraphs read, {len(entries)} raw entries, {len(unique)} unique")
    return unique


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: python scripts/index_volume.py <vol>  (e.g. 02)")
    vol = sys.argv[1].zfill(2)
    print(f"Indexing volume {vol}...")
    entries = index_volume(vol)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"tomo{vol}.jsonl"
    with out.open("w") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"Wrote {len(entries)} entries to {out.relative_to(PROJECT)}")
    print("\nFirst 5:")
    for e in entries[:5]:
        print(f"  leaf={e['leaf']:>4} p.{str(e['page_printed']):>5}  {e['title'][:30]:30}  | {e['context'][:90]}")


if __name__ == "__main__":
    main()
