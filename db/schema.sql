-- Schema for the madoz project (Balearic subset of Pascual Madoz's
-- Diccionario geográfico-estadístico-histórico de España, 1845-1850).
--
-- Two sources of entries live side by side:
--
-- 1) madoz_entries / madoz_tags / madoz_entry_tags — scraped from
--    diccionariomadoz.com (the curated WordPress mirror of Madoz).
--    Populated by scripts/scrape_madoz.py.
--
-- 2) chocr_entries — derived from our paragraph-based parsing of the
--    Internet Archive chOCR (Phase 1 of the pipeline). Populated by
--    scripts/load_chocr_index.py from data/index/all.jsonl and
--    data/index/from_scrape.jsonl.
--
-- The two sources are complementary: madoz_entries is the curated
-- ground truth (human-edited titles, structured fields) but is
-- incomplete; chocr_entries is exhaustive against the OCR but carries
-- OCR mangle. Phase 2 (Vision) will reconcile them against the page
-- images.

CREATE SEQUENCE IF NOT EXISTS seq_chocr_id START 1;

-- Diccionariomadoz.com mirror -------------------------------------------

CREATE TABLE IF NOT EXISTS madoz_entries (
    id                 INTEGER PRIMARY KEY,        -- WP post id
    slug               TEXT NOT NULL,
    title              TEXT NOT NULL,
    url                TEXT NOT NULL,
    date_published     TIMESTAMP,
    date_modified      TIMESTAMP,
    content_html       TEXT,
    content_text       TEXT,
    content_length     INTEGER,
    -- Heuristic-parsed fields. May be NULL for long articles
    -- (municipalities, partidos) where the leading sentence does not
    -- match a known template.
    place_type         TEXT,
    island             TEXT,
    judicial_district  TEXT,
    municipality       TEXT,
    fetched_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS madoz_tags (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL,
    slug   TEXT NOT NULL,
    count  INTEGER
);

CREATE TABLE IF NOT EXISTS madoz_entry_tags (
    entry_id INTEGER NOT NULL,
    tag_id   INTEGER NOT NULL,
    PRIMARY KEY (entry_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_madoz_entries_slug ON madoz_entries(slug);
CREATE INDEX IF NOT EXISTS idx_madoz_entries_island ON madoz_entries(island);
CREATE INDEX IF NOT EXISTS idx_madoz_entries_municipality
    ON madoz_entries(municipality);

-- chOCR-derived index ---------------------------------------------------

-- One row per entry we located in the Internet Archive chocr. `source`
-- distinguishes how the entry got here:
--   'regex'   — captured by scripts/index_volume.py
--   'scrape'  — present in madoz_entries but missed by our regex;
--               located in chocr by scripts/recover_missing.py
CREATE TABLE IF NOT EXISTS chocr_entries (
    id                 INTEGER PRIMARY KEY DEFAULT nextval('seq_chocr_id'),
    vol                TEXT NOT NULL,        -- '01' .. '16'
    leaf               INTEGER NOT NULL,
    page_printed       TEXT,
    title              TEXT NOT NULL,
    context            TEXT,
    source             TEXT NOT NULL DEFAULT 'regex',
    -- Optional link back to the curated source when we can pair them.
    madoz_entry_id     INTEGER,
    -- Structured fields, populated for scrape-sourced rows. Phase 3
    -- (Vision) will fill these for regex-sourced rows too.
    place_type         TEXT,
    island             TEXT,
    judicial_district  TEXT,
    municipality       TEXT
);

CREATE INDEX IF NOT EXISTS idx_chocr_entries_vol_leaf
    ON chocr_entries(vol, leaf);
CREATE INDEX IF NOT EXISTS idx_chocr_entries_title
    ON chocr_entries(title);
CREATE INDEX IF NOT EXISTS idx_chocr_entries_madoz_id
    ON chocr_entries(madoz_entry_id);

-- Vision-extracted entries (Phase 3) -----------------------------------

-- One row per Madoz entry, populated by Claude Vision over the page
-- image from Internet Archive. The fields here are the canonical
-- structured outputs of the project: cleaned title, structured place
-- attributes, free-text description, and the statistics fields that
-- the diccionariomadoz.com mirror is known to corrupt.
CREATE SEQUENCE IF NOT EXISTS seq_vision_id START 1;

CREATE TABLE IF NOT EXISTS vision_entries (
    id                 INTEGER PRIMARY KEY DEFAULT nextval('seq_vision_id'),
    vol                TEXT NOT NULL,
    leaf               INTEGER NOT NULL,
    page_printed       TEXT,
    -- Cleaned canonical title from Vision (e.g. "ARTA", "BELLVER",
    -- "POQUET (son)", "MARÍA (Santa)").
    title              TEXT NOT NULL,
    place_type         TEXT,                 -- predio / villa / cala / sierra...
    island             TEXT,                 -- Mallorca / Menorca / ...
    judicial_district  TEXT,                 -- Inca / Manacor / Palma / ...
    municipality       TEXT,                 -- Felanitx / Pollensa / ...
    description        TEXT,                 -- clean transcription of the body
    -- Statistics extracted from the entry. Stored as JSON so the
    -- numeric subfields (casas, vecinos, almas, ...) can grow without
    -- schema churn. Use JSON_EXTRACT for typed queries.
    stats              JSON,
    cross_references   TEXT[],               -- e.g. ["V. PALMA"]
    -- Vision's self-rated confidence: 'high' | 'medium' | 'low'.
    confidence         TEXT,
    -- Optional pointer back to the chocr paragraph and the curated
    -- scrape row, if we can match them confidently.
    chocr_entry_id     INTEGER,
    madoz_entry_id     INTEGER,
    -- Provenance for reproducibility.
    model              TEXT,                 -- e.g. 'claude-sonnet-4-6'
    extracted_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_vision_entries_vol_leaf
    ON vision_entries(vol, leaf);
CREATE INDEX IF NOT EXISTS idx_vision_entries_title
    ON vision_entries(title);
CREATE INDEX IF NOT EXISTS idx_vision_entries_island
    ON vision_entries(island);
CREATE INDEX IF NOT EXISTS idx_vision_entries_municipality
    ON vision_entries(municipality);

-- Text-extracted entries (Phase 3, OCR-text path) ----------------------

-- One row per Madoz entry, populated by Claude (Sonnet via API, or Opus
-- in-conversation under Max) reading the chocr plaintext for each leaf.
-- Same shape as vision_entries but kept separate so we never confuse
-- text-derived rows with image-derived ones — stats accuracy differs
-- (OCR digit corruption vs. visual ground truth).
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
    confidence         TEXT,                 -- 'high' | 'medium' | 'low'
    -- Multi-leaf window used at extraction time. 2 = target + 1 next
    -- leaf; 4 = mega-entry sliding window (PALMA, MAHON, …).
    window_size        INTEGER,
    -- Provenance for reproducibility.
    model              TEXT,                 -- e.g. 'claude-sonnet-4-6' or
                                             -- 'claude-opus-4-7-via-claude-code'
    source_file        TEXT,                 -- 'data/text/page_<vol>_<leaf>.json'
    note               TEXT,                 -- top-level "note" field, if any
    -- Optional links back to the regex index and curated mirror.
    chocr_entry_id     INTEGER,
    madoz_entry_id     INTEGER,
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
