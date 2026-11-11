# Pipeline, data layout and rebuild

How the edition is built: where everything lives, the phases of the
pipeline, and the commands to rebuild it. For the reasoning behind the
choices, and what was tried and dropped, see [METHOD.md](METHOD.md).

## What lives where

```
data/
  chocr/             # ABBYY hOCR per volume, from IA (gitignored, ~1 GB)
  page_numbers/      # leaf→page maps per volume (gitignored)
  txt_djvu/          # plain OCR text per volume (gitignored, ~100 MB)
  pdf/               # 16 IA facsimile PDFs (gitignored, 1.7 GB total).
                     # Required by the Tesseract validation pass; not
                     # used by the active chocr→text pipeline.
  pages/             # per-leaf JPEGs (gitignored, ~1 GB). Used only by
                     # the dormant Mapa pipeline (fetch_pages.py +
                     # build_gazetteer.py); not the active pipeline.
  text/_chocr/       # per-leaf chocr windows used by extraction
  text/              # per-leaf extracted JSON (one file per leaf, versioned)
  index/             # per-volume + merged JSONL indexes (versioned)
  tesseract/         # parallel Tesseract OCR output (gitignored, ~120 MB)
  ngib/              # NGIB toponym set for the dormant Mapa pipeline
                     # (gitignored)
  vision/            # legacy Vision-extraction batch output. The
                     # experiment was discarded (see METHOD.md, Difficulties §1);
                     # the table is gone. Kept for archival only.
db/
  schema.sql              # DuckDB schema (versioned)
  madoz.duckdb            # built DB (gitignored, regenerable)
scripts/                  # pipeline + maintenance scripts
web/
  index.html
  app.js
  style.css
  data.json               # flat export consumed by the static site
  abbreviations.json      # Madoz abbreviation glossary
```

The local DuckDB carries two complementary tables:

```
db/madoz.duckdb
├── chocr_entries           ← regex parse of IA hOCR              (1202 rows)
└── text_entries            ← Claude-extracted article bodies     (1217 rows)
```

`chocr_entries` is an exhaustive machine-readable index of the
facsimile that carries OCR noise in titles; `text_entries` is the
working canonical output that the
website renders. Each `text_entries` row carries `(vol, leaf,
page_printed)` for citation.

## Pipeline

Four phases. The earlier ones build a clean index; the later ones turn
that index into structured article bodies and ship them.

### Phase 1 — Index from chOCR (deterministic, free)

For each of the 16 volumes, download from Internet Archive:

- `_chocr.html.gz` (~64 MB) — compressed hOCR, one paragraph per Madoz
  entry. `id="page_LEAF"` per page tells us which leaf any paragraph
  belongs to.
- `_page_numbers.json` (~107 KB) — `leafNum → printed page number` map.
- `_djvu.txt` (~6 MB) — flat text, kept for ad-hoc grep.

`scripts/index_volume.py` parses the hOCR **by paragraph** (`<p
class="ocr_par">`), not as concatenated characters: each Madoz entry is
essentially one paragraph, so paragraph boundaries give free entry
segmentation. A regex pair (strict separator + loose fallback with
body-marker safeguard) plus a Balearic-context filter pull out the
Balearic paragraphs.

Output → `data/index/tomo<vol>.jsonl`, merged into
`data/index/all.jsonl`. One row per entry:

```json
{"vol": "02", "leaf": 603, "page_printed": "595",
 "title": "ARTA",
 "context": "V. de la isla de Mallorca, prov., aud. terr..."}
```

### Phase 2 — Claude text extraction over chocr (canonical path)

`scripts/stage_chocr.py` writes per-leaf chocr windows under
`data/text/_chocr/`. `scripts/extract_text.py` then walks every
Balearic leaf and asks Claude Sonnet 4.6 to:

- Locate the target entries on that leaf in the chocr text.
- Clean OCR glue (`deBaleares` → `de Baleares`, `v.dePalma` → `v. de
  Palma`).
- Preserve Madoz's abbreviation style (prov., aud. terr., part. jud.,
  térm., dióc., V. for *véase*, …).
- Output a structured JSON `{title, place_type, island,
  judicial_district, municipality, description, stats,
  cross_references, confidence}`.

One JSON file per leaf goes to `data/text/page_<vol>_<leaf>.json`;
`scripts/load_text.py` flattens them into `text_entries`. The
extraction prompt explicitly **skips numeric stats tables** (the chocr
mangles them; see
[Difficulties](METHOD.md#difficulties-what-didnt-work-and-why)).

### Phase 3 — Rescue + cleanup

`scripts/link_text_entries.py` cross-links every `text_entries` row to
its corresponding `chocr_entries` row (same `vol`/`leaf`, fuzzy title
match handling B↔V, accent strip, OCR digit confusions).

`scripts/rescue_unlinked.py` looks for `chocr_entries` paragraphs that
clearly describe a Balearic place but never produced a `text_entries`
row (because the extraction LLM dropped them, the OCR title was
mangled past the regex, etc.). Promotes them with
`confidence='unverified'` and `model='chocr-snippet'`.

`scripts/cleanup_unverified.py` applies a deterministic typographic
cleanup pass over the unverified rows (R/P→B in `Raleares` /
`Paleares`, glued abbreviations, OCR-junk characters, soft-hyphen
stitches) plus a hand-curated TITLE_FIXES table for OCR-mangled
lemmas (`AKL4NT` → `ARIANT`, `IUMIS (Son` → `RAMIS (Son)`, etc.).
Promotes the rows to `confidence='medium'` afterwards.

### Phase 4 — Independent OCR validation

**Why.** The corpus has no external ground-truth set to cross-check
against. Re-running the facsimile through a second, completely
independent OCR engine — one whose failure modes have no correlation
with ABBYY's — gives us that check. Two questions to answer empirically:

  1. Does ABBYY's hOCR (the source of `chocr_entries`) silently drop
     any Balearic article whose first paragraph it mangled past
     recognition? If so, the second engine should find it.
  2. Are the manual title corrections in `TITLE_FIXES` justified, or
     did we over-correct? An independent reading of the same page
     should agree on `AKL4NT` → `ARIANT`, `IUMIS (Son` → `RAMIS (Son)`,
     etc.

**How.** Tesseract 5 with the `spa` trained data set, run across all
16 IA facsimile PDFs. `scripts/tesseract_reocr_all.py` renders each
PDF page at 300 dpi and runs 10 parallel Tesseract workers (one per
M-series Performance core); on an Apple M4 Pro the full 11 894-page
corpus completes in ~50 minutes. Output: `data/tesseract/text/
tomoNN_pNNNN.txt` per page (~116 MB total, gitignored).

`scripts/verify_titles_tesseract.py` cross-checks each manual
`TITLE_FIXES` entry against the Tesseract reading of the
corresponding facsimile page (e.g. tom02 PDF page 560 confirms ABBYY's
`AKL4NT` is canonical `ARIANT`). `scripts/tesseract_full_xref.py`
runs the full-corpus comparison: every Tesseract-detected Balearic
opener against the DB. `scripts/tesseract_low_conf_sweep.py` runs the
same comparison but restricted to the ~66 pages where chocr_entries
shows obvious mangle markers (digits inside lemmas, II/H mismatches,
junk parens) — a faster targeted run when only the high-risk pages
need re-checking.

**What it found.** 0 novel Balearic articles missed by ABBYY (the
corpus is essentially complete), 2 cases where Tesseract reads the
canonical lemma cleaner than my hand-applied `TITLE_FIXES` (LLINARS →
LLINAS, RUMIS (Son) → RAMIS (Son) — Tesseract reads what Madoz
actually printed; I had over-corrected). A parallel run with Apple
Vision (`scripts/apple_vision_low_conf_sweep.py`, ~12× faster on the
Neural Engine) over the 66 mangle-rich pages reproduced the same
0-novel finding.

### Phase 5 — Web export + static site

`scripts/export_web_data.py` dumps `text_entries` to a single
`web/data.json` (~900 KB, 1217 entries). The site is a plain SPA —
vanilla JS, no framework, no DuckDB-WASM — that filters and renders
entries with their volume/page provenance and OCR-fix notes.

Tabs: **Inici** (project intro), **Explorar** (search + faceted
filter), **Estadístiques** (coverage tables: by island, place type,
judicial district, top 20 munis, volume coverage), **Demografia**
(inline SVG charts: top 20 munis by ànimes and by riquesa imponible,
ànimes-vs-riquesa slope graph, riquesa per capita, aggregated
population by island, productive infrastructure, contribution per
inhabitant), **Notes** (scholarly notes on the Diccionari de Madoz),
**Abreviatures** (Madoz abbreviation glossary).

### Dormant: geographic-coordinate pipeline

A separate set of scripts was written for an early "Mapa" tab that
plotted each entry on a Leaflet map. The tab was dropped because the
fuzzy geocoding produced too many hard-to-audit wrong placements.
The scripts are kept around in
case someone wants to revisit the problem with a different
methodology, but they're not part of the active pipeline:

  - `scripts/fetch_ngib.py` — pulls the Nomenclàtor Geogràfic de les
    Illes Balears toponym set from IDEIB.
  - `scripts/fetch_pages.py` — downloads per-leaf JPEGs from IA for
    visual verification of coordinate guesses.
  - `scripts/build_gazetteer.py` — builds a fuzzy match table between
    Madoz lemmas and NGIB toponyms.
  - `scripts/enrich_coords.py` — resolves lat/lon for each
    `text_entries` row using the gazetteer.

None of these scripts are required for the chocr→text→web pipeline
described above. The DB schema does not carry lat/lon columns.


## Usage

### One-time setup

```bash
pip install duckdb anthropic python-dotenv
# or:
uv venv && uv pip install -e .
```

Set `ANTHROPIC_API_KEY` in `.env` if you intend to re-run the LLM
extraction phase.

### Full rebuild from scratch

```bash
# 1. Download IA chocr + page_numbers + djvu for each volume (~1 GB)
for v in $(seq -w 1 16); do python scripts/fetch_volume.py $v; done

# 2. Build the per-volume regex index
for v in $(seq -w 1 16); do python scripts/index_volume.py $v; done
python scripts/merge_index.py

# 3. Load the chocr index into DuckDB
python scripts/load_chocr_index.py

# 4. LLM extraction over chocr text (costs API credits)
python scripts/stage_chocr.py            # writes data/text/_chocr windows
python scripts/extract_text.py           # per-leaf Sonnet extraction
python scripts/load_text.py              # flatten data/text/ into text_entries

# 5. Link + rescue + cleanup
python scripts/link_text_entries.py      # text↔chocr cross-link
python scripts/rescue_unlinked.py --apply       # promote chocr-only Balearic openers
python scripts/cleanup_unverified.py --apply    # OCR typographic cleanup

# 6. (Optional) Independent OCR validation via Tesseract
# Pre-requisite: the 16 IA facsimile PDFs under data/pdf/ (~1.7 GB
# total). They are gitignored. Volume 10 lives under a different IA
# identifier (`diccionariogeogr10madouoft`) — the loop handles it.
for v in 01 02 03 04 05 06 07 08 09 11 12 13 14 15 16; do
  curl -sL "https://archive.org/download/diccionariogeogr${v}mado/diccionariogeogr${v}mado.pdf" \
       -o "data/pdf/tomo${v}.pdf"
done
curl -sL "https://archive.org/download/diccionariogeogr10madouoft/diccionariogeogr10madouoft.pdf" \
     -o "data/pdf/tomo10.pdf"          # tom 10 — non-standard IA id

python scripts/tesseract_reocr_all.py --workers 10 --dpi 300   # ~50 min on M-series
python scripts/tesseract_full_xref.py                          # cross-check vs DB

# 7. Export the static site payload
python scripts/export_web_data.py
```

### Day-to-day

```bash
# Re-run Phase 1 on one volume after tweaking the regex
python scripts/index_volume.py 02 && python scripts/merge_index.py

# Refresh the DB after JSONL changes
python scripts/load_chocr_index.py

# Re-export the web payload after any text_entries change
python scripts/export_web_data.py

# Serve the site locally
python -m http.server -d web 8000
```

## Language convention

Code, scripts, commit messages and the README and docs are in English so the
project stays navigable for any contributor. The public-facing
website, `NOTES.md` (author's working notebook) and any
content-targeted docs are in Catalan, as a deliberate cultural choice
for the published artefact.

