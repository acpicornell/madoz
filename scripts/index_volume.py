"""Indexa un tom del Madoz aprofitant l'estructura del hOCR.

Fase 1 del pipeline "Madoz ben fet": donar, per a cada entrada balear,
la seva localització exacta dins l'edició original — tom, leaf, pàgina
printada — perquè la fase 2 (extracció amb Claude Vision) pugui anar
directament a la imatge correcta.

Estratègia (revisada per evitar la merdeta dels regex sobre text plàcat):

1. Llegir el chocr en streaming i emetre **paràgrafs** (`<p class=
   "ocr_par">`) per leaf. Cada paràgraf preserva la unitat editorial
   real del Madoz: cada entrada del diccionari és majoritàriament un sol
   paràgraf.
2. Per a cada paràgraf, comprovar si comença amb un títol Madoz
   (majúscules seguides de `:` o `;`).
3. Comprovar que el cos del paràgraf esmenti Balears amb un indicador
   geogràfic.
4. Resoldre `leafNum -> pàgina printada` via `page_numbers.json`.
5. Escriure JSON Lines.

Run: python scripts/index_volume.py <vol>   (per ex.: 02)
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

# Patrons sobre el text d'un paràgraf, no sobre el text concatenat del leaf.
# Madoz: "ARTA:", "BAÑALBUFAR:", "MARÍA (santa):", "ADAYA ó DADAYA:",
# "VICENCIO Y ESCADAS (San):". El títol pot incloure parentètics minúsculs
# darrere però comença sempre amb 2+ majúscules.
# Versió ESTRICTA: requereix separador (`:`, `;`, `-.`, `.-`) com a frontera
# entre títol i cos. Madoz canonic; matcha la majoria d'entrades.
PAT_ENTRY_STRICT = re.compile(
    r'^\s*'
    r'(?P<title>'
        r'[A-ZÑÁÉÍÓÚÜ][A-ZÑÁÉÍÓÚÜ0-9]{1,}'
        r"(?:[A-ZÑÁÉÍÓÚÜa-zñáéíóú0-9 \-,\.\']"
            r"|\([A-Za-zñáéíóúÑÁÉÍÓÚÜ0-9\s\.,\-\']{1,30}\)"
        r'){0,40}?'
    r')'
    r"\s*(?:[:;]\.?|-\.|\.\-)\s*[\'\"]?\s*"
    r'(?P<body>.+)',
    re.DOTALL,
)

# Versió LAXA: separador opcional. S'usa NOMÉS si l'estricta falla i com a
# safeguard exigim que el cos comenci amb un marker Madoz canonic (predio,
# v., cas., alq., etc.). Recupera entrades on l'OCR ha perdut el `:`.
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

# Markers d'inici de cos d'una entrada Madoz. Si el cos comença amb un
# d'aquests, la línia és quasi segur una entrada (i podem prescindir del
# separador `:` que l'OCR a vegades perd). Notar: les abreviatures (v.,
# cas., l., etc.) requereixen el punt literal; les paraules completes
# (predio, cala, etc.) s'acaben amb un caràcter no-lletra (espai, coma).
BODY_MARKER = re.compile(
    r'^\(?\s*(?:'
    # Paraules completes — han d'estar seguides d'un no-lletra
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
    # Abreviatures — el punt literal és el delimitador
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

# Títols que no són entrades (capçaleres, índex, errades pures).
TITLE_BLACKLIST = {
    "DICCIONARIO", "GEOGRAFICO", "GEOGRÁFICO", "HISTORICO", "HISTÓRICO",
    "ESPAÑA", "ULTRAMAR", "POSESIONES", "MADRID", "TOMO", "ADVERTENCIA",
    "PROLOGO", "PRÓLOGO", "INTRODUCCION", "INTRODUCCIÓN", "INDICE",
    "ÍNDICE", "MAPAS", "PESOS", "MEDIDAS", "ABREVIATURAS", "FIN",
}

# Patrons d'inici de pàgina/capçalera que volem descartar com a títol.
HEADER_TOKENS = re.compile(
    r'^(?:DICCIONARIO|GEOGRÁFICO|HISTÓRICO|ESPAÑA|ULTRAMAR|'
    r'POSESIONES|TOMO|FIN|ADVERTENCIA)\b',
    re.IGNORECASE,
)

PAGE_PAT = re.compile(r'class="ocr_page" id="page_(\d+)"')
PAR_OPEN_PAT = re.compile(r'<p class="ocr_par"')
CHAR_PAT = re.compile(r'<span class="ocrx_cinfo"[^>]*>([^<])</span>')


def iter_paragraphs(chocr_path: Path):
    """Yield (leaf:int, paragraph_text:str) per a cada paràgraf del tom.

    Streaming: no carrega tot el chocr a memòria. Aprofita que cada tag
    del format hOCR està en una línia separada al fitxer chocr d'IA.
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
        sys.exit(f"No hi és {chocr_path}. Executa abans: python scripts/fetch_volume.py {vol}")
    if not pn_path.exists():
        sys.exit(f"No hi és {pn_path}. Executa abans: python scripts/fetch_volume.py {vol}")

    pn = json.loads(pn_path.read_text())
    leaf2page = {p["leafNum"]: p.get("pageNumber") for p in pn["pages"]}

    entries: list[dict] = []
    n_par = 0
    for leaf, par_text in iter_paragraphs(chocr_path):
        n_par += 1
        if leaf is None or not par_text:
            continue
        # Normalitza espais. Manté el contingut íntegre.
        norm = re.sub(r"\s+", " ", par_text).strip()
        # Re-unir paraules trencades per guió de fi de línia: "Mallor-ca"
        # -> "Mallorca", "Ba-leares" -> "Baleares". Imprescindible per al
        # filtre balear, que d'altra manera perd les entrades on l'illa
        # cau just al trencament de columna.
        norm = re.sub(r"(\w)-(\w)", r"\1\2", norm)
        if len(norm) < 20:
            continue  # paràgraf massa curt per a una entrada real
        # Primer prova amb separador estricte (matcha la majoria d'entrades
        # canòniques). Si falla, prova la versió laxa amb safeguard al cos.
        m = PAT_ENTRY_STRICT.match(norm)
        require_body_marker = False
        if not m:
            m = PAT_ENTRY_LOOSE.match(norm)
            require_body_marker = True
        if not m:
            continue
        title = m.group("title").strip(" .,;:-")
        body = m.group("body").strip()
        # Filtres de qualitat del títol
        if not (3 <= len(title) <= 60):
            continue
        # Hem permès dígits dins el títol pels OCR-fixes (B1NIBASI), però
        # exigim almenys 3 lletres reals per evitar capçaleres tipus "B1" o
        # números d'apartat ("II", "III", "IV" — aquests són només 1-3 caps
        # però amb 0-3 lletres).
        n_letters = sum(1 for c in title if c.isalpha())
        if n_letters < 3:
            continue
        if HEADER_TOKENS.match(title):
            continue
        if title.upper() in TITLE_BLACKLIST:
            continue
        # Eviti títols que són una seqüència de lletres soltes ("S. C.")
        if re.fullmatch(r"(?:[A-ZÑÁÉÍÓÚÜ]\.?\s*){1,3}", title):
            continue
        # Safeguard de la versió laxa: si no hi havia separador, el cos ha
        # de començar amb un marker Madoz canonic.
        if require_body_marker and not BODY_MARKER.match(body):
            continue
        # Filtre balear: l'OCR a vegades pega articles ("laisla", "delas")
        # i el `\b` falla. Apliquem space-insertion abans del search per
        # recuperar entrades on l'illa està al body amb particula enganxada.
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

    # Dedupliquem per (vol, leaf, title_norm)
    seen: set[tuple] = set()
    unique: list[dict] = []
    for e in entries:
        key = (e["leaf"], e["title"].upper())
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    print(f"  {n_par} paràgrafs llegits, {len(entries)} entrades crues, {len(unique)} úniques")
    return unique


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Ús: python scripts/index_volume.py <vol>  (per ex.: 02)")
    vol = sys.argv[1].zfill(2)
    print(f"Indexant tom {vol}...")
    entries = index_volume(vol)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"tomo{vol}.jsonl"
    with out.open("w") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"Escrit {len(entries)} entrades a {out.relative_to(PROJECT)}")
    print("\nPrimeres 5:")
    for e in entries[:5]:
        print(f"  leaf={e['leaf']:>4} p.{str(e['page_printed']):>5}  {e['title'][:30]:30}  | {e['context'][:90]}")


if __name__ == "__main__":
    main()
