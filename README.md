# Madoz, Balearic subset

[![Deploy](https://github.com/acpicornell/madoz/actions/workflows/deploy.yml/badge.svg)](https://github.com/acpicornell/madoz/actions/workflows/deploy.yml)

A re-digitisation of the **Balearic Islands subset** of Pascual
Madoz's *Diccionario geográfico-estadístico-histórico de España y sus
posesiones de Ultramar* (Madrid, 1845–1850, 16 vols.). This edition
rebuilds every Balearic article from the Internet Archive facsimile as
a clean, citable, structured transcription.

**Live site: <https://madoz.corpusbalear.org>** — part of
[Corpus Balear](https://corpusbalear.org).

Side-project segregated from
[Nomenclator](https://github.com/acpicornell/nomenclator); see
[docs/METHOD.md](docs/METHOD.md) for its goals.

## What's in it

- **1 217** Balearic articles (`text_entries`), 1 041 high and 176
  medium confidence, over ~715 `(vol, leaf)` pairs.
- Volume / leaf / printed-page metadata on every entry, so it can be
  cited as *"Madoz, t. II, p. 595"* and checked against the facsimile.
- Structured place metadata (place type, island, judicial district,
  municipality) and inline statistics for 99 entries (8 %).
- Validated against a second OCR engine over all 11 894 pages of the 16
  volumes: 0 novel Balearic articles missed by ABBYY.

## How it was made

ABBYY hOCR from the Internet Archive is parsed paragraph by paragraph
into a regex index of Balearic entries. Claude then extracts each
article body from that OCR text (not from page images), preserving
Madoz's abbreviations; a deterministic rescue and cleanup pass follows,
and an independent Tesseract / Apple Vision re-OCR checks completeness
and title fixes. The result is exported as a single JSON consumed by a
vanilla-JS static site.

## Run locally

```bash
uv venv && uv pip install -e .
python scripts/export_web_data.py      # re-export web/data.json from the DB
python -m http.server -d web 8000      # serve the site
```

The full rebuild from the Internet Archive sources is in
[docs/PIPELINE.md](docs/PIPELINE.md#usage).

## Documentation

- [docs/PIPELINE.md](docs/PIPELINE.md) — data layout, the five pipeline
  phases, the dormant coordinate pipeline, setup and rebuild commands,
  language convention.
- [docs/METHOD.md](docs/METHOD.md) — goals, current status figures, and
  the difficulties met (what didn't work, and why).
- [web/README.md](web/README.md) — the static site (in Catalan).

## License

Code is licensed under **AGPL-3.0-or-later** (see `LICENSE`). If you
run a modified version of this software as a network service, you must
offer the source of your modifications to the users of that service.

Underlying data carries the licence of its origin: the Madoz facsimile
(Internet Archive scans of an 1845–1850 work) is public domain.
