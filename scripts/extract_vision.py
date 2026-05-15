"""Phase 3: extract structured entries from Madoz page images via Claude Vision.

For each (vol, leaf) page with chocr_entries rows, send the page JPEG to
Claude Sonnet 4.6 along with a list of expected titles (from our chocr
index) and ask for structured JSON: one record per entry with cleaned
title, place attributes, full description and parsed statistics. The
key value-add over chocr/scrape sources is that Vision can see the
original facsimile and correct numeric errors the OCR mirrors carry.

Modes:
  --page VOL LEAF       extract a single page (testing)
  --sample              run on a small diverse curated sample
  --all                 process every unfetched page in chocr_entries

Output: data/vision/page_<vol>_<leaf>.json — one file per page.

Requires `ANTHROPIC_API_KEY` in the environment.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import anthropic
import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
PAGES_DIR = PROJECT / "data" / "pages"
OUT_DIR = PROJECT / "data" / "vision"

MODEL = "claude-sonnet-4-6"

# Tool schema: the model is forced to call this with a structured list
# of entries. Anthropic tool-use guarantees the JSON shape on success.
ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": (
                "Cleaned canonical title as printed in the facsimile, "
                "in ALL CAPS for the main name with any parenthetical "
                "specifier preserved literally (e.g. 'ARTA', 'POQUET (son)', "
                "'MARÍA (Santa)', 'ALEGRE (son, antes Cova den Panxe)')."
            ),
        },
        "place_type": {
            "type": "string",
            "description": (
                "Spanish lemma for the entry type. Pick the most specific "
                "physical or built feature when the entry leads with one "
                "('predio', 'alquería', 'villa', 'lugar', 'caserío', "
                "'cala', 'punta', 'cabo', 'sierra', 'valle', 'monte', "
                "'isla', 'islote', 'castillo', 'torre', 'parroquia', "
                "'feligresía', 'arroyo', 'cueva', 'ermita', 'fuente'). "
                "Fall back to admin units only when the entry is itself "
                "an admin unit ('provincia', 'diócesis', 'partido judicial', "
                "'audiencia', 'tercio marítimo')."
            ),
        },
        "island": {
            "type": "string",
            "enum": ["Mallorca", "Menorca", "Ibiza", "Formentera", "Cabrera"],
        },
        "judicial_district": {
            "type": "string",
            "description": (
                "Partido judicial. One of the canonical 1845 districts: "
                "Inca, Manacor, Palma, Mahón, Ciudadela, Ibiza. Null if "
                "the entry does not name one."
            ),
        },
        "municipality": {
            "type": "string",
            "description": (
                "Term/jurisdiction the entry sits in (e.g. 'Felanitx', "
                "'Pollensa', 'Manacor'). Null if not specified."
            ),
        },
        "description": {
            "type": "string",
            "description": (
                "Full cleaned transcription of the entry's body, in "
                "modern Spanish orthography, preserving abbreviations as "
                "Madoz wrote them ('prov.', 'aud. terr.', 'c. g.', "
                "'part. jud.', 'térm.', 'felig.', 'V.' for véase). Fix "
                "obvious OCR errors. Keep punctuation."
            ),
        },
        "stats": {
            "type": "object",
            "description": (
                "Statistics block at the end of the entry, if any. Include "
                "only the fields the facsimile actually shows; use integers "
                "for counts. Common keys: 'casas', 'vecinos', 'almas', "
                "'habitantes', 'contribucion', 'riqueza_imponible'."
            ),
            "additionalProperties": True,
        },
        "cross_references": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Other Madoz entries this one points to via 'V. X' "
                "(véase). Empty array if none."
            ),
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": (
                "Your confidence in the structured fields. 'low' means "
                "the OCR was too poor to read reliably or the entry is "
                "fragmented; 'medium' for moderate ambiguity; 'high' for "
                "clean, unambiguous extraction."
            ),
        },
    },
    "required": ["title", "description", "confidence"],
    "additionalProperties": False,
}

TOOL = {
    "name": "record_page_entries",
    "description": (
        "Record every Madoz dictionary entry visible on this page as a "
        "structured record. One call, with the full list of entries."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "items": ENTRY_SCHEMA,
                "description": "All Balearic entries visible on this page.",
            },
        },
        "required": ["entries"],
    },
}

SYSTEM_PROMPT = """\
You extract structured records from facsimile pages of Pascual Madoz's
"Diccionario geográfico-estadístico-histórico de España y sus posesiones
de ultramar" (Madrid, 1845-1850). The two-column pages contain multiple
short dictionary entries; each entry begins with its title in SMALL CAPS
followed by a colon or semicolon, and ends before the next title.

For this project we care only about Balearic entries — those whose
geographic context names Mallorca, Menorca, Ibiza (Iviza), Formentera or
Cabrera. Skip non-Balearic entries that happen to share the page.

When you read the page, prefer the printed image over any pre-OCR'd
hint we provide: OCR garbles digits and Spanish accents routinely. Your
main job is to recover the correct counts (casas, vecinos, almas,
contribución) the facsimile shows.

Call the `record_page_entries` tool exactly once with the full list.\
"""


def load_chocr_entries_for(vol: str, leaf: int) -> list[dict]:
    con = duckdb.connect(str(DB), read_only=True)
    rows = con.execute(
        """SELECT title, context, source FROM chocr_entries
           WHERE vol = ? AND leaf = ?
           ORDER BY title""",
        [vol, leaf],
    ).fetchall()
    return [{"title": r[0], "context": r[1], "source": r[2]} for r in rows]


def get_page_printed(vol: str, leaf: int) -> str | None:
    con = duckdb.connect(str(DB), read_only=True)
    row = con.execute(
        "SELECT page_printed FROM chocr_entries WHERE vol = ? AND leaf = ? LIMIT 1",
        [vol, leaf],
    ).fetchone()
    return row[0] if row else None


def build_user_message(
    vol: str,
    leaf: int,
    page_printed: str | None,
    chocr_entries: list[dict],
    image_b64: str,
) -> list[dict]:
    """Build the content blocks for the user message: hints + image."""
    titles_hint = "\n".join(
        f"  - {e['title']}  (chocr source: {e['source']})"
        for e in chocr_entries
    )
    header = (
        f"Volume tom {vol}, leaf {leaf}, printed page {page_printed or '?'}.\n\n"
        f"Our prior pass identified these {len(chocr_entries)} Balearic "
        f"entries on this page (titles may carry OCR noise; trust the "
        f"facsimile):\n{titles_hint}\n\n"
        f"Extract every Balearic entry actually visible on the page. "
        f"Add any we missed; drop any we mis-flagged."
    )
    return [
        {"type": "text", "text": header},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": image_b64,
            },
        },
    ]


def extract_page(
    client: anthropic.Anthropic,
    vol: str,
    leaf: int,
) -> dict:
    """Run a single Vision call for the given page; return the parsed result."""
    image_path = PAGES_DIR / f"tomo{vol}_leaf{leaf}.jpg"
    if not image_path.exists():
        raise FileNotFoundError(f"Page image missing: {image_path}")

    chocr_entries = load_chocr_entries_for(vol, leaf)
    page_printed = get_page_printed(vol, leaf)
    image_b64 = base64.standard_b64encode(image_path.read_bytes()).decode()

    user_content = build_user_message(
        vol, leaf, page_printed, chocr_entries, image_b64
    )

    msg = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "record_page_entries"},
        messages=[{"role": "user", "content": user_content}],
    )

    # Find the tool_use block and lift its input.
    payload = None
    for block in msg.content:
        if block.type == "tool_use" and block.name == "record_page_entries":
            payload = block.input
            break
    if payload is None:
        raise RuntimeError(
            "Model did not call the expected tool. content blocks: "
            + str([b.type for b in msg.content])
        )

    return {
        "vol": vol,
        "leaf": leaf,
        "page_printed": page_printed,
        "model": MODEL,
        "usage": {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
        },
        "entries": payload["entries"],
    }


def write_result(result: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"page_{result['vol']}_{result['leaf']}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return out


def pick_sample() -> list[tuple[str, int]]:
    """Return a diverse curated sample for prompt validation."""
    con = duckdb.connect(str(DB), read_only=True)
    # A handful of well-known anchor pages spanning small/large entries,
    # all four islands and several OCR-mangle classes.
    targets = [
        ("ARTA",            "02", 603),  # canonical villa we recovered
        ("PALMA",           "12", None), # giant multi-page entry
        ("ALFABIA",         "01", 537),  # sierra (formerly skipped)
        ("VINATER (jo)",    "16", 342),  # predi with mangled parens
        ("MAHON",           "11", None), # large city entry
        ("FORMENTERA",      "08", 146),  # island article
        ("IBIZA",           "09", 379),  # island article
        ("ALEGRE rSON«",    "01", 530),  # heavy OCR mangle
        ("FELAN1TX",        "08", None), # digit-substitution case
        ("POQUET (son)",    "13", 157),  # missing-separator case
    ]
    out: list[tuple[str, int]] = []
    for title, vol, leaf_hint in targets:
        if leaf_hint is not None:
            out.append((vol, leaf_hint))
            continue
        row = con.execute(
            "SELECT vol, leaf FROM chocr_entries WHERE title LIKE ? LIMIT 1",
            [f"{title}%"],
        ).fetchone()
        if row:
            out.append((row[0], row[1]))
    # Deduplicate while preserving order
    seen: set = set()
    sample: list[tuple[str, int]] = []
    for pair in out:
        if pair not in seen:
            seen.add(pair)
            sample.append(pair)
    return sample


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_mutually_exclusive_group(required=True)
    sub.add_argument("--page", nargs=2, metavar=("VOL", "LEAF"),
                     help="extract a single page (e.g. --page 02 603)")
    sub.add_argument("--sample", action="store_true",
                     help="run on a curated diverse sample")
    sub.add_argument("--all", action="store_true",
                     help="process every page in chocr_entries not yet done")
    ap.add_argument("--overwrite", action="store_true",
                    help="re-extract pages even if their JSON already exists")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set in environment.")
    client = anthropic.Anthropic()

    if args.page:
        vol, leaf = args.page[0], int(args.page[1])
        pairs = [(vol, leaf)]
    elif args.sample:
        pairs = pick_sample()
        print(f"Sample size: {len(pairs)}")
    else:  # --all
        con = duckdb.connect(str(DB), read_only=True)
        pairs = [(r[0], r[1]) for r in con.execute(
            "SELECT DISTINCT vol, leaf FROM chocr_entries ORDER BY vol, leaf"
        ).fetchall()]
        print(f"All pages: {len(pairs)}")

    total_in = total_out = 0
    for vol, leaf in pairs:
        out_path = OUT_DIR / f"page_{vol}_{leaf}.json"
        if out_path.exists() and not args.overwrite:
            print(f"  [skip] {out_path.name} already exists")
            continue
        try:
            print(f"  [GET]  tom{vol} leaf{leaf}...", flush=True)
            result = extract_page(client, vol, leaf)
        except FileNotFoundError as e:
            print(f"  [skip] {e}")
            continue
        except Exception as e:
            print(f"  [fail] tom{vol} leaf{leaf}: {e}", file=sys.stderr)
            continue
        out = write_result(result)
        ti = result["usage"]["input_tokens"]
        to = result["usage"]["output_tokens"]
        total_in += ti
        total_out += to
        print(f"  [ok]   {out.name}: {len(result['entries'])} entries "
              f"(in={ti} out={to} toks)")
        # Polite small pause to avoid bursts
        time.sleep(0.2)

    if total_in or total_out:
        # Sonnet 4.6 pricing: $3 / M input, $15 / M output (May 2026).
        # Image tokens count as input.
        cost = total_in / 1_000_000 * 3 + total_out / 1_000_000 * 15
        print()
        print(f"Tokens: in={total_in:,}  out={total_out:,}")
        print(f"Estimated cost (non-batch Sonnet 4.6): ${cost:.4f}")


if __name__ == "__main__":
    main()
