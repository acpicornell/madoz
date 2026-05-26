# Madoz, Balearic subset

A re-digitisation of the **Balearic Islands subset** of Pascual
Madoz's *Diccionario geográfico-estadístico-histórico de España y sus
posesiones de Ultramar* (Madrid, 1845–1850, 16 vols.).

Side-project segregated from
[Nomenclator](https://github.com/acpicornell/nomenclator); see
`NOTES.md` for the original motivation.

## Goals

This project builds a Balearic index of Madoz directly from primary
sources — the Internet Archive facsimile scans and their ABBYY hOCR —
and uses Claude to re-extract structured article bodies from that
text. Concretely, every entry should have:

- A clean transcription of the article body, with Madoz's
  abbreviation style preserved.
- Volume / leaf / printed-page metadata, so the entry can be cited as
  *"Madoz, t. II, p. 595"* and verified against the facsimile.
- Structured place metadata (place_type, island, judicial district,
  municipality) where the article supports it.
- Structured statistics (vecinos, almas, riqueza imponible,
  contribución, productive infrastructure counts) where they appear
  inline in the article prose.

Source of truth is the Internet Archive facsimile alone. An earlier
version of this project also indexed a third-party WordPress mirror
([diccionariomadoz.com](https://diccionariomadoz.com)) as a parallel
transcription set, but the mirror was removed: its licence is unclear
and the upstream project has not been updated since 2023. The 1217-
entry corpus has been independently validated against a second OCR
engine (Tesseract 5 + Apple Vision) to confirm it captures every
Balearic article ABBYY's hOCR could plausibly recover from the facsimile.

## What lives where

```
data/
  chocr/             # hOCR per volume (gitignored, ~1 GB total)
  page_numbers/      # leaf→page maps per volume (gitignored)
  txt_djvu/          # plain OCR text per volume (gitignored)
  pdf/               # 16 IA facsimile PDFs (gitignored, 1.7 GB total).
                     # Required by the Tesseract validation pass; not
                     # used by the active chocr→text pipeline.
  pages/             # page JPEGs (gitignored)
  text/_chocr/       # per-leaf chocr windows used by extraction
  text/              # per-leaf extracted JSON (one file per leaf, versioned)
  index/             # per-volume + merged JSONL indexes (versioned)
  tesseract/         # parallel Tesseract OCR output (gitignored)
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
mangles them; see "Difficulties").

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

Second OCR pass via Tesseract 5 (`spa` trained data) across all 16
volumes. `scripts/tesseract_reocr_all.py` renders each PDF page at
300 dpi and runs 10 parallel Tesseract workers; on an Apple M4 Pro
the full corpus completes in ~50 minutes.

`scripts/verify_titles_tesseract.py` cross-checks each manual
`TITLE_FIXES` entry against the Tesseract reading of the
corresponding facsimile page (e.g. tom02 PDF page 560 confirms ABBYY's
`AKL4NT` is canonical `ARIANT`). `scripts/tesseract_full_xref.py`
runs the full-corpus comparison: every Tesseract-detected Balearic
opener against the DB, surfacing any article ABBYY missed
(0 in the current run — the corpus is essentially complete).

### Phase 5 — Web export + static site

`scripts/export_web_data.py` dumps `text_entries` to a single
`web/data.json` (~900 KB, 1217 entries). The site is a plain SPA —
vanilla JS, no framework, no DuckDB-WASM — that filters and renders
entries with their volume/page provenance and OCR-fix notes.

Tabs: **Home** (project intro), **Explore** (search + faceted filter),
**Estadístiques** (coverage tables: by island, place type, judicial
district, top 20 munis, volume coverage), **Demografia** (inline SVG
charts: top 20 munis by ànimes and by riquesa imponible, ànimes-vs-
riquesa slope graph, riquesa per capita, aggregated population by
island, productive infrastructure,
contribution per inhabitant), **Notes** (working notebook).

## Current status

| Metric | Value |
|---|---:|
| `text_entries` rows | **1 217** |
| Confidence high / medium / unverified | 1 041 / 176 / 0 |
| Distinct `(vol, leaf)` pairs covered | ~715 |
| Entries with structured `stats` | 99 (8 %) |
| `chocr_entries` total (raw OCR index) | 1 202 |
| Tesseract validation pages re-OCRed | 11 894 (all 16 volumes) |
| Novel Balearic articles found by Tesseract | 0 (corpus complete) |

## Difficulties (what didn't work, and why)

A blow-by-blow of the surprises and the rules of thumb they produced.
Keeping this here so future-us doesn't re-learn it.

### 1. Vision over page images was worse than text over chocr

The intuition was that **visual ground truth** would beat OCR text, so
we sent the JPEGs to Claude Sonnet 4.6 via the Batch API (~$10 for the
Balearic set). Result: Sonnet routinely extracted **the wrong
peninsular homonym** sharing a column with our target (e.g. `PORCUNA`
instead of `POQUET (son)`), invented or skipped sections, and was
inconsistent across runs. The output was unsalvageable; the batch was
discarded and the Vision tables removed from the maintained pipeline.
The chocr-text path with a careful system prompt gives much better
cleanups, presumably because the model can rely on linear positional
cues (article ordering, leaf boundaries) instead of spatially decoding
a two-column 350-dpi facsimile.

Lesson: for serial OCR'd prose, **clean the text input then prompt
carefully** beats Vision on the raw image, at least at current model
strength.

### 2. Local OCR-only LLMs were a fiasco

We briefly evaluated running local-OCR models on the JPEGs as a
cheaper alternative to either ABBYY hOCR or Claude (DeepSeek OCR,
OLM-OCR variants, MLX 8-bit quantisations on the Mac). All of them
performed substantially worse than ABBYY on 19th-century column-set
Spanish prose: dropped lines, hallucinated content, slow throughput.
The benchmarks and weights are not retained.

Lesson: **don't replace ABBYY for this corpus.** For pre-modern
typography in column layouts, a tuned classical OCR is still ahead of
generalist vision-language models.

### 3. Numeric stats tables: chocr-mangled, can't trust them

Madoz's per-municipality statistics tables (casas, vecinos, almas,
riqueza imponible, contribución, molinos…) are typeset as multi-column
numeric grids that ABBYY hOCR garbles unrecoverably. The extraction
prompt now explicitly **skips them** and inserts a bracketed `[Madoz
inclou aquí una taula d'estadístiques…]` placeholder; the `stats` JSON
column is only populated when the figures appear inline in prose. A
regex pass over `description` recovers a few more (`pobl.: X vec., Y
alm.`, `CONTR.: X rs.`, `RIQ. IMP.: …`), bringing total coverage to
99 / 1 183 (8 %) — enough to power the Demografia charts and
spot-check, but not enough for quantitative whole-archipelago
analysis.

Lesson: **don't promise quantitative figures from OCR'd 19th-century
statistical tables.** Either pay for proper Vision + human review, or
mark the column unavailable.

### 4. Multi-leaf mega-articles got silently truncated

PALMA, MAHON, IBIZA, ALCUDIA, MANACOR (part. jud.), CIUDADELA, … are
multi-leaf articles. The leaf-by-leaf extraction in Phase 3 caps
output per call, so anything past the first leaf's allowance was cut.
The per-leaf JSON file looked "complete" but the article body was
truncated, sometimes severely (PALMA captured the wrong paragraph
entirely; see case 5 below).

Fix: `recover_municipality_articles.py` reads a ±4-leaf chocr window
around each candidate so the model can see continuation, and re-asks
for the **full body of the named target only**, ignoring adjacent
peninsular homonyms. The script is idempotent and only touches rows
whose description is < 1/2 of the curated mirror's length (a signal
for "we truncated this").

Lesson: **always size the context window to the article, not to the
page.** Where articles cross leaves, sliding-window context beats
strict per-leaf isolation.

### 5. PALMA was completely wrong

The two PALMA rows (`part. jud.` + `c.`) had captured the tail of the
peninsular article *Valles y Revilla; Peral y Pinilla* + raw OCR table
noise, because Sonnet jumped to the wrong paragraph when no clear
"PALMA" header appeared in the chocr window. We hand-transcribed both
from chocr leaves 12/586 + 12/588 and froze them in `recover_palma.py`
so re-running the pipeline doesn't overwrite them. The script is a
template for any other "model picked the wrong article" case.

Lesson: **for landmark articles, keep a small `recover_<name>.py`
escape hatch with hand-verified text.** Cheaper than tightening the
general prompt.

### 6. MAHON ended up triplicated

MAHON city was extracted three times — once correctly from leaf 25,
once as a duplicate from leaf 30, once as half of a leaf 30 pair — and
the duplicate carried the "HISTORIA" section the original lacked.
Spotted only when the Demografia bar chart showed `MAHON 13 280` twice
in the top 20. Fix: extract the missing HISTORIA from the duplicate,
append to the canonical row, delete the dup, re-export. The pattern
made it into `dedup_municipality_articles.py` for any future copies.

Lesson: **chart your own data.** A duplicate that looks fine in the
table jumps out instantly in a sorted bar chart.

### 7. OCR titles disagree with the canonical form

Cases hit so far: `CASCONCOS` → `CAS-CONCOS`, `BINIBECA` →
`BINI-BECA`, `BINISALEM` (Madoz actually prints `BENISALEM`),
`BINISAFULLA ó BINI-SAFAYA`, `CUEVALARGA` → `CUEVA-LARGA`,
`LLUGALGARI` → `LLUCALCARI`, `SAN LORENZO ó LLORENS ÜESCARDASAR` →
`SAN LORENZO ó LLORENS DESCARDASAR`, plus the OCR-mangled rescue set
(`AKL4NT` → `ARIANT`, `ARIA5íV` → `ARIANY`, `BOSCII` → `BOSCH`,
`F1ÜL` → `FIOL`, `LLE.\An\E` → `LLENAIRE`, `LLI.NWS` → `LLINAS`,
`LLOBACII` → `LLOBACH`, `PEDRÜXELLA (cnAN)` → `PEDRUXELLA (Gran)`,
`PERPlHA` → `PERPIÑA`, `IUMIS (Son` → `RAMIS (Son)`,
`HA SOS ÍCADO DE)` → `BAJOS (cabo de)`).

`scripts/cleanup_unverified.py` carries the canonical fix list as a
`TITLE_FIXES = {text_entry_id: (new_title, reason), …}` dict so the
same correction never needs hand-applying twice. The fixes are
cross-checked by `scripts/verify_titles_tesseract.py`, which re-OCRs
the corresponding PDF page with Tesseract `spa` and reports whether
the cleaner reading matches the manual fix.

Note on policy: we preserve Madoz's own typos verbatim (the IA
facsimile is the source of truth, not the curated mirror's "natural"
reading). The audit catches OCR misreads; it does not "correct"
Madoz.

Lesson: **for OCR-derived titles, treat any below-threshold similarity
hit as either a real OCR typo to fix or a real homonym to verify.**
Don't silently let the index carry an OCR-only title forever; don't
silently "improve" Madoz either.

### 8. Homonyms across volumes

`LLUCALCARI` (aldea de Mallorca, depende de Deyá, mid=116020) vs.
`LLUCALARI (SAN ANTONIO DE)` (Menorca/Alayor, mid=116018) — easy to
mis-link because the strings differ by one character and one volume.
`SALAS` (Orense) appears next to `SALAS (isleta)` (Cabrera). `PALMA`
(de Mallorca) vs. `PALMA` (de Canarias) on adjacent leaves. The
`audit_homonyms.py` report cross-checks the count of Balearic articles
per leaf in chocr against `text_entries` and flags missing ones.

Lesson: **a place can share a name with another place; verify against
the article body, not just the title.**

### 9. Volume 10 lives at a different IA identifier

Volume 10 is hosted under `diccionariogeogr10madouoft` instead of the
regular `diccionariogeogr10mado`. The current `fetch_volume.py` will
404 on this volume; fetch the three files manually with `curl` if you
re-run the full pipeline. See git history for the workaround.

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

Code, scripts, commit messages and this README are in English so the
project stays navigable for any contributor. The public-facing
website, `NOTES.md` (author's working notebook) and any
content-targeted docs are in Catalan, as a deliberate cultural choice
for the published artefact.

## License

Code is licensed under **AGPL-3.0-or-later** (see `LICENSE`). If you
run a modified version of this software as a network service, you must
offer the source of your modifications to the users of that service.

Underlying data carries the licence of its origin: the Madoz facsimile
(Internet Archive scans of an 1845–1850 work) is public domain.
