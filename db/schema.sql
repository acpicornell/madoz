-- Schema for the madoz project (Balearic subset of Pascual Madoz's
-- Diccionario geográfico-estadístico-histórico de España, 1845-1850).
--
-- Two tables hold the working corpus:
--
-- 1) chocr_entries — derived from our paragraph-based parsing of the
--    Internet Archive chOCR (Phase 1 of the pipeline). Populated by
--    scripts/load_chocr_index.py from data/index/all.jsonl.
--
-- 2) text_entries  — the canonical output: one row per Madoz article,
--    populated by Claude (Sonnet via API, or Opus in-conversation under
--    the Max plan) reading the chocr plaintext for each leaf and
--    extracting a structured record. This is what the static website
--    consumes.

CREATE SEQUENCE IF NOT EXISTS seq_chocr_id START 1;

-- chOCR-derived index ---------------------------------------------------

-- One row per entry we located in the Internet Archive chocr.
-- `source` is currently always 'regex' (captured by
-- scripts/index_volume.py). A legacy 'nomenclator' variant existed in
-- the days of the mirror-recovery scripts; it is no longer produced.
CREATE TABLE IF NOT EXISTS chocr_entries (
    id                 INTEGER PRIMARY KEY DEFAULT nextval('seq_chocr_id'),
    vol                TEXT NOT NULL,        -- '01' .. '16'
    leaf               INTEGER NOT NULL,
    page_printed       TEXT,
    title              TEXT NOT NULL,
    context            TEXT,
    source             TEXT NOT NULL DEFAULT 'regex',
    place_type         TEXT,
    island             TEXT,
    judicial_district  TEXT,
    municipality       TEXT
);

CREATE INDEX IF NOT EXISTS idx_chocr_entries_vol_leaf
    ON chocr_entries(vol, leaf);
CREATE INDEX IF NOT EXISTS idx_chocr_entries_title
    ON chocr_entries(title);

-- Text-extracted entries (active path, OCR-text + Claude) ---------------

-- Canonical output. One row per Madoz article, populated by Claude
-- reading the chocr plaintext for each leaf. This is what the static
-- website consumes via scripts/export_web_data.py.
CREATE SEQUENCE IF NOT EXISTS seq_text_id START 1;

CREATE TABLE IF NOT EXISTS text_entries (
    id                 INTEGER PRIMARY KEY DEFAULT nextval('seq_text_id'),
    vol                TEXT NOT NULL,
    leaf               INTEGER NOT NULL,
    page_printed       TEXT,
    title              TEXT NOT NULL,        -- as cleaned by the LLM
    place_type         TEXT,
    island             TEXT,
    judicial_district  TEXT,
    municipality       TEXT,
    description        TEXT,                 -- normalised; what the web shows
    description_raw    TEXT,                 -- LLM's first-pass extraction, kept for provenance; NEVER shown on the web
    stats              JSON,                 -- {casas,vecinos,almas,…}
    cross_references   TEXT[],
    confidence         TEXT,                 -- 'high' | 'medium' | 'low' | 'unverified'
    -- Multi-leaf window used at extraction time. 2 = target + 1 next
    -- leaf; 4 = mega-entry sliding window (PALMA, MAHON, …).
    window_size        INTEGER,
    -- Provenance for reproducibility.
    model              TEXT,                 -- e.g. 'claude-sonnet-4-6' or
                                             -- 'claude-opus-4-7-via-claude-code'
                                             -- or 'chocr-snippet' / 'tesseract-snippet'
                                             -- for rescue-promoted rows
    source_file        TEXT,                 -- 'data/text/page_<vol>_<leaf>.json'
    note               TEXT,                 -- top-level "note" field, if any
    chocr_entry_id     INTEGER,              -- optional link to chocr_entries
    extracted_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_text_entries_vol_leaf
    ON text_entries(vol, leaf);
CREATE INDEX IF NOT EXISTS idx_text_entries_title
    ON text_entries(title);
CREATE INDEX IF NOT EXISTS idx_text_entries_island
    ON text_entries(island);
CREATE INDEX IF NOT EXISTS idx_text_entries_municipality
    ON text_entries(municipality);
