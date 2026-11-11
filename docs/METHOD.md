# Method, status and difficulties

What the edition sets out to do, where it stands, and the lessons learnt
building it. The pipeline itself is described in [PIPELINE.md](PIPELINE.md).

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

Source of truth is the Internet Archive facsimile alone. The 1217-
entry corpus has been independently validated against a second OCR
engine (Tesseract 5 + Apple Vision) to confirm it captures every
Balearic article ABBYY's hOCR could plausibly recover from the facsimile.

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
whose description is short enough that "we truncated this" is the
most likely explanation.

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

Note on policy: we preserve Madoz's own typos verbatim. The IA
facsimile is the source of truth; the audit catches OCR misreads, it
does not "correct" Madoz.

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

