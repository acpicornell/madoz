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
#
# Two categories of in-title OCR noise we explicitly tolerate inside
# the *initial caps run* (the lead word):
#
# 1) Punctuation/symbol noise — stripped post-match by
#    ``_strip_title_noise`` (``F'ORMENTÓR`` → ``FORMENTÓR``,
#    ``CABRER^A`` → ``CABRERA``).
#
# 2) Lowercase glyphs the IA ABBYY OCR routinely produces in place of
#    uppercase letters in this corpus:
#      - ``a`` ↔ ``A`` (``SaN JUAN`` → ``SAN JUAN``, ``GaYA`` → ``GAYA``,
#        ``MaSTAQUERA`` → ``MASTAQUERA``)
#      - ``h`` ↔ ``E`` (``FhRREDELLS`` → ``FERREDELLS``, downstream
#        ``fix_title_mismatches.py`` rewrites the ``H`` to ``E``)
#      - ``ü`` ↔ ``Ü``/``O`` (``FüRMENTO`` → ``FÜRMENTO``, downstream
#        accent fix rewrites to ``FORMENTO``)
#    These are uppercased in ``_strip_title_noise`` so the captured
#    title still reads as caps; the lemma-to-canonical mapping is the
#    job of the existing ``fix_ocr_*`` family. Calibrated empirically
#    against tomos 6/8/11/13/15/16 — different set from minano (BSB
#    scans had ``hnu``; IA ABBYY has ``ah`` + the ``ü`` ↔ ``Ü`` accent
#    quirk shared with minano).
#
# Kept intentionally small: the wider the set the more false positives
# from peninsular paragraphs whose all-caps shouting words begin with a
# lowercase glyph.
TITLE_NOISE_CHARS = r"\^_'"
TITLE_LOWER_TOLERATED = r"ahü"
TITLE_NOISE_RE = re.compile(f'[{TITLE_NOISE_CHARS}]+')


_LEMMA_RE = re.compile(r"^[^\s\(\)\-]+")


def _strip_title_noise(title: str) -> str:
    """Remove ``^_'`` from a captured title and uppercase the tolerated
    lowercase-OCR letters — applied only to the **lead lemma** (the
    first whitespace-/paren-/hyphen-delimited token).

    Madoz titles routinely carry a legitimate lowercase parenthetical
    specifier (``ACEITE (cala del)``, ``ALBERCUIX (fortaleza de)``,
    ``CAYETANO (San, vulgo LA CAPELLA)``) and OCR sometimes drops the
    opening ``(`` entirely (``BARGAS c \\i.\\ DE las)`` — closing
    paren but no opener), so splitting on ``(`` alone is unsafe. The
    lead lemma is where the OCR confusion we target lives
    (``FhRREDELLS``, ``GaYA``, ``FüRMENTO``, ``FUFOXa``), so transform
    only that first token and leave anything after the first space
    untouched.
    """
    m = _LEMMA_RE.match(title)
    if not m:
        return title
    head = m.group(0)
    tail = title[len(head):]
    head = TITLE_NOISE_RE.sub('', head).translate(
        {ord(c): ord(c.upper()) for c in TITLE_LOWER_TOLERATED}
    )
    return head + tail


PAT_ENTRY_STRICT = re.compile(
    r'^[\s\'\"~`«»_,\.\-\(\)\[\]\{\}t0\d]{0,6}'  # leading OCR junk
    r'(?P<title>'
        r'[A-ZÑÁÉÍÓÚÜ][A-ZÑÁÉÍÓÚÜ0-9' + TITLE_NOISE_CHARS + TITLE_LOWER_TOLERATED + r']{1,}'  # initial caps run (>=2)
        r'[^:;\n]{0,50}?'                       # title body (anything but the separator)
    r')'
    r"\s*(?:[:;]\.?|-\.|\.\-)\s*[\'\"]?\s*"
    r'(?P<body>.+)',
    re.DOTALL,
)

# LOOSE version: separator is optional / non-standard (the OCR drops or
# mangles the canonical `:` into `,`, `.`, `•`, or pure whitespace).
# Used ONLY when STRICT fails; combined with a body-marker safeguard
# (the body must start with a canonical Madoz marker: predio, v., cas.,
# alq., etc.).
#
# Title structure made explicit: initial caps run, then optional
# additional caps-only words (separated by spaces, possibly joined by
# lowercase 'ó' / 'o' for alternates like "LLORETO ó LLORITO"), then an
# optional parenthetical specifier. This lets us absorb the parens into
# the title even when the separator that follows is missing.
PAT_ENTRY_LOOSE = re.compile(
    r'^[\s\'\"~`«»_,\.\-\(\)\[\]\{\}t0\d]{0,6}'
    r'(?P<title>'
        r'[A-ZÑÁÉÍÓÚÜ][A-ZÑÁÉÍÓÚÜ0-9' + TITLE_NOISE_CHARS + TITLE_LOWER_TOLERATED + r']{1,}'  # initial caps run
        r'(?:\s+(?:[óòo]\s+)?[A-ZÑÁÉÍÓÚÜ' + TITLE_NOISE_CHARS + r']+(?:[\-,\'][A-ZÑÁÉÍÓÚÜ]+)*)*'   # extra caps words
        r'(?:\s*\([A-Za-zñáéíóúÑÁÉÍÓÚÜ0-9\s\.,\-\']{1,30}\))?'  # optional parens
    r')'
    r'[\s\.\,•:;\'\"]{1,4}'                                   # flexible separator
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
    r'departamento|distrito|cuart[óo]n|porci[óo]n|territorio|terreno|antigua|antiguo|'
    r'pequeña|pequeño|pequeñas|pequeños|cortijada|caser[ií]a|granja|señoría|'
    r'cab|cabezada|colina|laguna|saliente|punto|piedra|peñ[óo]n|peñas?|nombre|'
    r'pedazo|porci[óo]n|huerta|isletas?|edificio|capilla|ermita|fort[ií]n|'
    r'castillo|fortaleza|atalaya|mirador|pico|sembrad|labranza|dehesa|coto'
    r')(?=[^A-Za-zñáéíóúÑÁÉÍÓÚÜ])'
    r'|'
    # Abbreviations — the literal period is the delimiter
    r'(?:v|l|cas|c|ald|alq|parr|felig|r|hac|desp|prov|distr|cot|ant|cap|t|fr|s)\.'
    # Generic "en la/el [Madoz-token]" — Madoz entry continuations that
    # have lost their initial type marker (e.g. LLORETO body starts with
    # "en la isla y dióc. de Mallorca"). Safe enough since the second
    # word constrains.
    r'|en\s+(?:la|el|las|los)\s+(?:isla|prov|part|c|villa|d[ií]óc|aud|cuart|t[eé]rm|playa|sierra|puerto|cabo|punta|cala|monte|valle|tercio|aldea|estancia|barrio|granja)'
    r')',
    re.IGNORECASE,
)

# Balearic filter — double-signal rule:
#
#   - Single CANONICAL match (incl. abbreviations) → accept
#   - Otherwise, accept only if 2+ DISTINCT mentions exist in the body
#     (canonical and/or fuzzy). Two different mangles or one canonical
#     + one fuzzy both count.
#
# Rationale: a real Balearic entry always says "isla de X, prov. de
# Baleares..." so it has at minimum 2 references close together; a
# continental entry that hits one fuzzy by chance has only the spurious
# match. Empirically, a single-signal fuzzy filter ran 1 TP / 11 FP on
# six volumes; double-signal flipped it to 3 TP / 0 FP across all 16.

# Canonical pattern: a Madoz geographic marker within 40 chars of a
# Balearic name (exact or legitimate abbreviation).
PAT_BALEAR_CANON = re.compile(
    r'\b(?:isla|isl\.|prov\.|adm\.|dióc\.|partido|cala|punta|cabo|sierra|'
    r'valle|bahia|bahía|monte|playa|puerto|tercio|distr\.|distrito|'
    r'mar[ií]t(?:imo)?\.?|aud\.|c\. g\.)\.?'
    r'.{0,40}?'
    r'(?:Mallorca|Menorca|Ibiza|Iviza|Formentera|Cabrera|'
    r'Baleares|Raleares|Paleares|Balea\b|'
    r'Mall\.|Men\.|Form\.|Cabr\.)',
    re.IGNORECASE | re.DOTALL,
)

# Fuzzy-only mentions. Anchored with \b on both ends to avoid matching
# substrings inside continental words (e.g. "Elvira" → "lvira" used to
# trigger the Iviza fuzzy; "pulcros" triggered Baleares fuzzy). The
# Baleares position-1 class stays restrictive at [a4] (the "Huleares"
# case is handled by an explicit alternative below).
PAT_BALEAR_FUZZY = re.compile(
    r'\b(?:'
        r'M[aá][li1!|tj]{2}[oa0][rnti][ceoa][aá]|'          # Mallorca-mangled
        r'M[eé][ni1!|m]{1,2}[oa0][rnti][ceoa][aá]|'          # Menorca-mangled
        r'[Ili1!|jJ][bdh][i1!|jl][zsxv][a4o]|'               # Ibiza-mangled
        r'[Ili1!|jJ]v[i1!|jl][zsxv][a4]|'                    # Iviza-mangled
        r'F[oa0]rm[eé][nuim]t[ecoa][rnti][a4o]|'             # Formentera-mangled
        r'C[a4]br[ecoa][rnti][a4]|'                          # Cabrera-mangled
        # Baleares with documented mangles: position 1 is conservative
        # ([a4]) plus an explicit "Huleares" alt; capital class catches
        # B/R/P/D/I/H/S confusion.
        r'[BRPDIS][a4]l[ecoa]{1,2}r[ecoa]s|'
        r'H[ua]l[ecoa]{1,2}r[ecoa]s|'                        # Huleares-style (u allowed only after H)
        r'[BRPDIS][a4]l[ecoa][a4]'                           # Balea-mangled
    r')\b',
    re.IGNORECASE,
)

# Any-mention pattern (canonical OR fuzzy), used to count distinct hits.
PAT_BALEAR_ANY = re.compile(
    r'\b(?:'
        r'Mallorca|Menorca|Ibiza|Iviza|Formentera|Cabrera|'
        r'Baleares|Raleares|Paleares|Balea|'
        r'Mall\.|Men\.|Form\.|Cabr\.|'
        r'M[aá][li1!|tj]{2}[oa0][rnti][ceoa][aá]|'
        r'M[eé][ni1!|m]{1,2}[oa0][rnti][ceoa][aá]|'
        r'[Ili1!|jJ][bdh][i1!|jl][zsxv][a4o]|'
        r'[Ili1!|jJ]v[i1!|jl][zsxv][a4]|'
        r'F[oa0]rm[eé][nuim]t[ecoa][rnti][a4o]|'
        r'C[a4]br[ecoa][rnti][a4]|'
        r'[BRPDIS][a4]l[ecoa]{1,2}r[ecoa]s|'
        r'H[ua]l[ecoa]{1,2}r[ecoa]s|'
        r'[BRPDIS][a4]l[ecoa][a4]'
    r')\b',
    re.IGNORECASE,
)


def passes_balear(body_search: str) -> bool:
    """Double-signal Balearic filter.

    Accept if either (a) canonical match exists near a Madoz marker, or
    (b) two distinct mentions (canonical+fuzzy or fuzzy+fuzzy) coexist
    in the body. Distinct = different match text (case-insensitive).
    """
    if PAT_BALEAR_CANON.search(body_search):
        return True
    hits = {m.group(0).lower() for m in PAT_BALEAR_ANY.finditer(body_search)}
    return len(hits) >= 2

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
        raw_title = m.group("title")
        # Caps-dominance filter on the lead lemma. With TITLE_LOWER_TOLERATED
        # enabled the regex now also matches Titlecase words like ``Banco``,
        # ``Cartajeoa``, ``Para`` (from statistics-appendix prose), which are
        # not Madoz headwords. A real OCR-damaged lemma has 1–2 lowercase
        # glyphs in an otherwise-CAPS lead word (``FhRREDELLS``, ``GaYA``,
        # ``FüRMENTO``); a Titlecase sentence opener has 4+ lowercase. The
        # ratio test (≥ 60 % uppercase in the alphabetic chars of the
        # first whitespace-/paren-/hyphen-delimited token) cuts the line
        # cleanly between the two.
        lemma_m = _LEMMA_RE.match(raw_title)
        if lemma_m:
            lemma = lemma_m.group(0)
            alpha = [c for c in lemma if c.isalpha()]
            if alpha:
                upper_ratio = sum(1 for c in alpha if c.isupper()) / len(alpha)
                if upper_ratio < 0.6:
                    continue
        title = _strip_title_noise(raw_title).strip(" .,;:-")
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
        if not passes_balear(body_search):
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
