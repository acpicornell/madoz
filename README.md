# Madoz, done right

A re-digitalisation of the **Balearic subset** of Pascual Madoz's
*Diccionario geográfico-estadístico-histórico de España y sus posesiones
de Ultramar* (Madrid, 1845–1850, 16 vols.), aiming for higher accuracy
and traceability than existing online transcriptions.

Side-project segregated from [Nomenclator](https://github.com/acpicornell/nomenclator);
see `NOTES.md` (in Catalan) for the original motivation.

## Why

The current source used by Nomenclator (`diccionariomadoz.com`) has two
problems:

1. **Numeric errors** introduced by human transcription on top of older
   OCR. Example: for Maria de la Salut, it reads *"363 casas, 975 vec."*
   while the facsimile says *"262 casas, 275 vec."* Not safe for
   quantitative analysis.
2. **Incomplete coverage** (~95–97% of the canonical Balearic entries).
   Canonical entries such as **ARTA**, **BELLVER**, **DEYA**, **REFAL**
   and several `(son …)` / `(can …)` predis are missing from it.
3. **No volume/page metadata**, which makes academic citation
   ("Madoz, t. II, p. 595") impossible.

This project rebuilds the index from primary sources (Internet Archive
scans + ABBYY hOCR), then plans to run Claude Vision over the original
page images for high-quality structured extraction.

## Pipeline

Two phases:

### Phase 1 — Indexing (deterministic, free)

For each of the 16 volumes, download from Internet Archive:

- `_chocr.html.gz` (~64 MB) — compressed hOCR with `id="word_LEAF_INDEX"`
  per word, so we can recover which leaf any text fragment belongs to.
- `_page_numbers.json` (~107 KB) — `leafNum → printed page number` map
  (calibration confidence ~96%).
- `_djvu.txt` (~6 MB) — flat text, kept for ad-hoc grep.

Parse the hOCR **by paragraph** (`<p class="ocr_par">`), not by
concatenated characters: each Madoz entry is essentially one paragraph,
so paragraph boundaries give us free entry segmentation. Then apply a
regex pair (strict separator + loose fallback with body-marker
safeguard) plus a Balearic context filter.

Output: `data/index/tomo<vol>.jsonl` per volume, then
`data/index/all.jsonl` merged + deduplicated. One row per entry:

```json
{"vol": "02", "leaf": 603, "page_printed": "595",
 "title": "ARTA",
 "context": "V. de la isla de Mallorca, prov., aud. terr..."}
```

### Phase 2 — High-quality extraction (Claude Vision)

Not yet implemented. For each indexed entry, download the page image
from Archive.org (`page/n{LEAF}_w1600.jpg`, ~350 dpi source) and send it
to Claude Vision with a structured-output prompt. Multi-page entries
(PALMA, MAHON) get the full image range as context.

## Current status

Phase 1 is complete. The index has:

- **1058 unique Balearic entries** across the 16 volumes.
- Distribution: Mallorca 902, Menorca 84, Ibiza ~30, Formentera ~9,
  Cabrera 2, Baleares (generic) 15, suspicious 28.
- vs Nomenclator's curated DB (1053 entries from diccionariomadoz.com):
  - 755 strict matches, 831 fuzzy (OCR-aware ~78% recall).
  - 286 entries we have that Nomenclator misses — including major
    Mallorcan place names (ARTA, BELLVER, DEYA, ESCORCA + the REFAL
    family) confirmed absent from diccionariomadoz.com.
  - ~220 entries Nomenclator has that we still miss, mostly due to
    extreme OCR mangling (e.g. `B1NIBASI` → `BINIBASI`, `LLUGHMAYOR` →
    `LLUCHMAYOR`). These are recoverable by phase 2.

## Usage

```bash
# 1. Fetch a volume's OCR sources from Internet Archive (~70 MB / volume)
python scripts/fetch_volume.py 02

# 2. Index one volume (writes data/index/tomo02.jsonl)
python scripts/index_volume.py 02

# 3. After indexing several volumes, merge + dedupe
python scripts/merge_index.py
# -> data/index/all.jsonl
```

To rebuild the entire index from scratch:

```bash
for v in $(seq -w 1 16); do
  python scripts/fetch_volume.py $v
  python scripts/index_volume.py $v
done
python scripts/merge_index.py
```

Volume 10 needs a special alternate Internet Archive identifier
(`diccionariogeogr10madouoft`) — see commit history. The current
`fetch_volume.py` will 404 on it; fetch the three files manually with
`curl` if you re-run the full pipeline.

## Layout

```
data/
  chocr/             # hOCR per volume (gitignored, ~1 GB total)
  page_numbers/      # leaf→page maps per volume (gitignored)
  txt_djvu/          # plain OCR text per volume (gitignored)
  pages/             # page JPEGs for phase 2 (gitignored)
  index/             # per-volume + merged JSONL indexes (versioned)
scripts/
  fetch_volume.py    # downloads chocr/page_numbers/djvu from IA
  index_volume.py    # paragraph-based hOCR parser + Balearic filter
  merge_index.py     # merges per-volume JSONL into all.jsonl
NOTES.md             # author's Catalan working notebook
```

## Language convention

Code, scripts, commits, and this README are in English so the project
is navigable for any contributor. The public-facing website (when it
exists) will be in Catalan, as a deliberate cultural choice for the
published artefact. `NOTES.md` stays in Catalan — it's the author's
working notebook, not a contributor onboarding doc.

## License

TBD.
