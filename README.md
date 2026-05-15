# Madoz, done right

A re-digitalisation of the **Balearic subset** of Pascual Madoz's
*Diccionario geográfico-estadístico-histórico de España y sus posesiones
de Ultramar* (Madrid, 1845–1850, 16 vols.), aiming for higher accuracy
and traceability than existing online transcriptions.

Side-project segregated from [Nomenclator](https://github.com/acpicornell/nomenclator);
see `NOTES.md` (in Catalan) for the original motivation.

## Why

The current source used by Nomenclator (`diccionariomadoz.com`) has
three problems:

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

## Architecture

The project keeps two independent sources of entries side by side in a
local DuckDB (`db/madoz.duckdb`):

```
db/madoz.duckdb
├── madoz_entries           ← scraped from diccionariomadoz.com (1152 rows)
├── madoz_tags              ← scraped WP tags
├── madoz_entry_tags        ← scraped entry↔tag links
└── chocr_entries           ← our derived index (1194 rows)
    ├── source='regex'         (1161, found by index_volume.py)
    └── source='nomenclator'   (33, found by recover_from_nomenclator.py)
```

The two sources are complementary: `madoz_entries` is curated ground
truth (human-edited titles, structured place_type / island / district /
municipality fields) but is incomplete; `chocr_entries` is exhaustive
against the OCR but carries OCR mangle in the titles. Phase 2 (Vision)
will reconcile them against the original page images.

## Pipeline

Three phases. Only phases 1 + 2 are implemented today.

### Phase 1 — Indexing from chOCR (deterministic, free)

For each of the 16 volumes, download from Internet Archive:

- `_chocr.html.gz` (~64 MB) — compressed hOCR with one paragraph per
  Madoz entry. `id="page_LEAF"` per page lets us know which leaf any
  paragraph belongs to.
- `_page_numbers.json` (~107 KB) — `leafNum → printed page number`
  map (calibration confidence ~96%).
- `_djvu.txt` (~6 MB) — flat text, kept for ad-hoc grep.

`index_volume.py` parses the hOCR **by paragraph** (`<p class=
"ocr_par">`), not by concatenated characters: each Madoz entry is
essentially one paragraph, so paragraph boundaries give us free entry
segmentation. A regex pair (strict separator + loose fallback with
body-marker safeguard) plus a Balearic-context filter pull out the
~1161 Balearic paragraphs.

Output per volume → `data/index/tomo<vol>.jsonl`. Merged into
`data/index/all.jsonl`. One row per entry:

```json
{"vol": "02", "leaf": 603, "page_printed": "595",
 "title": "ARTA",
 "context": "V. de la isla de Mallorca, prov., aud. terr..."}
```

### Phase 2 — Scrape + recover (cross-reference)

`scrape_madoz.py` pulls the curated entries from
diccionariomadoz.com's WP REST API (raw JSONL cached under
`data/madoz/`) and loads them into `madoz_entries`. Politely paced
(1.5 s between requests, exponential backoff on 429/5xx).

`recover_from_nomenclator.py` then takes the ~58 curated entries that
Phase 1 missed (after Lev-fuzzy dedup) and tries to locate each in the
chocr by fuzzy-matching the title against paragraph heads. The ones it
places get tagged `source='nomenclator'` and emitted to
`data/index/from_nomenclator.jsonl`; the ones nobody can find go to
`data/index/unrecoverable.jsonl`.

`load_chocr_index.py` loads both JSONLs into the `chocr_entries`
table.

### Phase 3 — Vision over the page images (not implemented)

For each `chocr_entries` row, fetch the page image from Archive.org
(`page/n{LEAF}_w1600.jpg`, ~350 dpi source) and send it to Claude
Vision with a structured-output prompt. Multi-page entries (PALMA,
MAHON) get the full image range as context. Vision will:

- Clean OCR-mangled titles to canonical form.
- Pull out the statistics fields (casas, vecinos, habitantes) the
  facsimile shows, fixing the human-transcription errors the source
  site carries.
- Resolve duplicates between `source='regex'` and `source='nomenclator'`
  entries on the same paragraph.

## Current status

Phase 1 + 2 complete. The combined index has **1194 unique Balearic
entries**:

| Bucket | Count |
|---|---:|
| Found by regex over chocr | 1161 |
| Imported from diccionariomadoz via fuzzy match | 33 |
| Documented losses (in neither chocr nor diccionariomadoz) | 9 |
| Effective coverage of the ~1150-1200 canonical universe | **~99%** |

Distribution by island (from chocr regex pass): Mallorca 902,
Menorca 84, Ibiza ~30, Formentera ~9, Cabrera 2, Baleares (generic) 15.

## Usage

### One-time setup

```bash
# Install duckdb (the only third-party dep)
pip install duckdb

# Or with uv:
uv venv && uv pip install -e .
```

### Full rebuild from scratch

```bash
# 1. Download IA chocr + page_numbers + djvu for each volume (~1 GB total)
for v in $(seq -w 1 16); do python scripts/fetch_volume.py $v; done

# 2. Build the per-volume regex index
for v in $(seq -w 1 16); do python scripts/index_volume.py $v; done

# 3. Merge per-volume into data/index/all.jsonl
python scripts/merge_index.py

# 4. Scrape diccionariomadoz.com (or skip if data/madoz/posts.jsonl exists)
python scripts/scrape_madoz_extras.py   # only the mis-categorised slugs
python scripts/scrape_madoz.py          # full scrape; ~30 min, polite pacing
# (alternative: --from-cache to skip the network)

# 5. Locate missing-from-ours entries in the chocr and union the index
python scripts/recover_from_nomenclator.py

# 6. Load both sources into the local DuckDB
python scripts/load_chocr_index.py
```

After (6), `db/madoz.duckdb` has both `madoz_entries` and
`chocr_entries` populated; `data/index/combined.jsonl` is the union of
the two for downstream consumption.

### Quick day-to-day

```bash
# Re-run Phase 1 on one volume (e.g. after tweaking the regex)
python scripts/index_volume.py 02 && python scripts/merge_index.py

# Refresh the DB after JSONL changes
python scripts/scrape_madoz.py --from-cache   # rebuilds madoz_entries
python scripts/load_chocr_index.py            # rebuilds chocr_entries
```

### Volume 10 caveat

Volume 10 lives under an alternate Internet Archive identifier
(`diccionariogeogr10madouoft`) instead of the regular
`diccionariogeogr10mado`. The current `fetch_volume.py` will 404 on
this volume; fetch the three files manually with `curl` if you re-run
the full pipeline. See commit history for the workaround we used.

## Layout

```
data/
  chocr/             # hOCR per volume (gitignored, ~1 GB total)
  page_numbers/      # leaf→page maps per volume (gitignored)
  txt_djvu/          # plain OCR text per volume (gitignored)
  pages/             # page JPEGs for phase 3 (gitignored)
  index/             # per-volume + merged JSONL indexes (versioned)
  madoz/             # raw WP REST scrape (versioned)
db/
  schema.sql              # DuckDB schema (versioned)
  madoz.duckdb            # built DB (gitignored, regenerable)
scripts/
  fetch_volume.py             # downloads chocr/page_numbers/djvu from IA
  index_volume.py             # paragraph-based hOCR parser + Balearic filter
  merge_index.py              # merges per-volume JSONL into all.jsonl
  scrape_madoz.py             # WP REST scraper for diccionariomadoz.com
  scrape_madoz_extras.py      # recovers mis-categorised slugs by slug
  recover_from_nomenclator.py # locates curated entries we missed in chocr
  load_chocr_index.py         # loads all.jsonl + from_nomenclator.jsonl into DB
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
