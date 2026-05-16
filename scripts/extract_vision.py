"""Phase 3: extract structured entries from Madoz page images via Claude Vision.

For each (vol, leaf) page with chocr_entries rows, send the page JPEG to
Claude Sonnet 4.6 along with a list of expected titles (from our chocr
index) and ask for structured JSON: one record per entry with cleaned
title, place attributes, full description and parsed statistics. The
key value-add over chocr/scrape sources is that Vision can see the
original facsimile and correct numeric errors the OCR mirrors carry.

Modes:
  --page VOL LEAF       extract a single page (sync, for testing)
  --sample              sync run on a curated diverse sample
  --all                 sync run on every page in chocr_entries (slow + $$)
  --batch-submit        build a Batch-API job for every unprocessed page
                        and submit it (returns batch_id, runs async on
                        Anthropic's side — laptop can be closed). Splits
                        into chunks so each batch fits the API size cap.
  --batch-status        list active/recent batches and their progress
  --batch-fetch         download results for a specific batch_id (or all
                        finished batches from data/vision/.batches/)
                        and write per-page JSON files.

Output: data/vision/page_<vol>_<leaf>.json — one file per page.

Requires `ANTHROPIC_API_KEY` in the environment.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
from pathlib import Path

import anthropic
import duckdb
from dotenv import load_dotenv
from PIL import Image

# Pull ANTHROPIC_API_KEY from a project-local .env if one exists. Never
# log or print the value. `.env` is gitignored; see .env.example for the
# expected variable names.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
PAGES_DIR = PROJECT / "data" / "pages"
OUT_DIR = PROJECT / "data" / "vision"
BATCH_REGISTRY = OUT_DIR / ".batches"  # one JSON per submitted batch

# Anthropic's vision pipeline downsamples to 1568px on the long edge
# before processing, so sending larger doesn't add information — but
# inflates payload size 5×. Pre-resize to fit cleanly under the Batch
# API's per-batch size cap and to speed up the upload.
VISION_MAX_DIM = 1568
# JPEG quality 82 keeps the printed text readable while halving the
# payload vs. q88. Madoz pages are high-contrast B/W type, so heavy
# compression doesn't visibly degrade letter shapes.
VISION_JPEG_QUALITY = 82
# Anthropic Batch API caps batch files at 256 MB; we keep a generous
# margin so we never see a 413.
BATCH_PAYLOAD_BUDGET = 180 * 1024 * 1024

DEFAULT_MODEL = "claude-sonnet-4-6"
# Pricing per million tokens (May 2026 list rates, USD).
MODEL_PRICING = {
    "claude-opus-4-7":           {"in": 15.00, "out": 75.00},
    "claude-sonnet-4-6":         {"in": 3.00,  "out": 15.00},
    "claude-haiku-4-5-20251001": {"in": 1.00,  "out": 5.00},
}

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


def encode_resized_image(image_path: Path) -> str:
    """Read a JPG, downsample so the long edge ≤ VISION_MAX_DIM, return base64.

    Vision processes at ~1568px internally; bigger uploads waste payload
    without improving accuracy. Cached to ``data/pages_resized/`` so a
    second submit doesn't redo the work.
    """
    cache_dir = PROJECT / "data" / "pages_resized"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / image_path.name
    if cached.exists() and cached.stat().st_mtime >= image_path.stat().st_mtime:
        return base64.standard_b64encode(cached.read_bytes()).decode()
    img = Image.open(image_path)
    if max(img.size) > VISION_MAX_DIM:
        img.thumbnail((VISION_MAX_DIM, VISION_MAX_DIM), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=VISION_JPEG_QUALITY, optimize=True)
    cached.write_bytes(buf.getvalue())
    return base64.standard_b64encode(buf.getvalue()).decode()


def build_request_params(
    vol: str,
    leaf: int,
    page_printed: str | None,
    chocr_entries: list[dict],
    image_b64: str,
    model: str,
) -> dict:
    """Return the ``params`` dict for a Batch API request item."""
    return {
        "model": model,
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "tools": [TOOL],
        "tool_choice": {"type": "tool", "name": "record_page_entries"},
        "messages": [
            {
                "role": "user",
                "content": build_user_message(
                    vol, leaf, page_printed, chocr_entries, image_b64
                ),
            }
        ],
    }


def custom_id_for(vol: str, leaf: int) -> str:
    return f"page_{vol}_{leaf}"


def split_into_chunks(items: list, get_size) -> list[list]:
    """Group items into chunks whose total ``get_size(item)`` ≤ the budget."""
    chunks: list[list] = [[]]
    cur = 0
    for item in items:
        sz = get_size(item)
        if chunks[-1] and cur + sz > BATCH_PAYLOAD_BUDGET:
            chunks.append([])
            cur = 0
        chunks[-1].append(item)
        cur += sz
    return chunks


def submit_batches(
    client: anthropic.Anthropic,
    model: str,
    overwrite: bool,
) -> list[str]:
    """Build per-page Batch requests, split by payload size, submit each chunk.

    Writes a small record per submitted batch to ``data/vision/.batches/``
    so the user can fetch results later from any machine.
    """
    con = duckdb.connect(str(DB), read_only=True)
    pairs = [(r[0], r[1]) for r in con.execute(
        "SELECT DISTINCT vol, leaf FROM chocr_entries ORDER BY vol, leaf"
    ).fetchall()]

    suffix = "" if model == DEFAULT_MODEL else f"_{model.split('-')[1]}"
    to_do = []
    for vol, leaf in pairs:
        out_path = OUT_DIR / f"page_{vol}_{leaf}{suffix}.json"
        if out_path.exists() and not overwrite:
            continue
        img_path = PAGES_DIR / f"tomo{vol}_leaf{leaf}.jpg"
        if not img_path.exists():
            print(f"  [skip] missing image: {img_path.name}")
            continue
        to_do.append((vol, leaf, img_path))

    print(f"Pages to process: {len(to_do)} (of {len(pairs)} total)")
    if not to_do:
        print("Nothing to submit — everything already has output JSON.")
        return []

    # Build all request dicts in memory once; chunk by approximate JSON
    # byte size (base64 image dominates, so a coarse len() suffices).
    requests: list[tuple[dict, int]] = []
    for i, (vol, leaf, img_path) in enumerate(to_do):
        chocr = load_chocr_entries_for(vol, leaf)
        page_printed = get_page_printed(vol, leaf)
        b64 = encode_resized_image(img_path)
        params = build_request_params(
            vol, leaf, page_printed, chocr, b64, model
        )
        req = {"custom_id": custom_id_for(vol, leaf), "params": params}
        # Conservative size estimate: serialize once.
        size = len(json.dumps(req))
        requests.append((req, size))
        if (i + 1) % 50 == 0 or i + 1 == len(to_do):
            print(f"  [build] {i+1}/{len(to_do)} requests prepared")

    chunks = split_into_chunks(requests, get_size=lambda r: r[1])
    print(f"Split into {len(chunks)} batch(es) "
          f"(payload budget {BATCH_PAYLOAD_BUDGET/1024/1024:.0f} MB each)")

    BATCH_REGISTRY.mkdir(parents=True, exist_ok=True)
    batch_ids: list[str] = []
    for k, chunk in enumerate(chunks, start=1):
        bytes_total = sum(s for _, s in chunk)
        only_reqs = [r for r, _ in chunk]
        print(f"\n[batch {k}/{len(chunks)}] {len(only_reqs)} requests, "
              f"{bytes_total/1024/1024:.1f} MB — submitting…")
        batch = client.messages.batches.create(requests=only_reqs)
        batch_ids.append(batch.id)
        rec = {
            "batch_id": batch.id,
            "model": model,
            "submitted_at": batch.created_at.isoformat() if hasattr(batch.created_at, "isoformat") else str(batch.created_at),
            "request_count": len(only_reqs),
            "custom_ids": [r["custom_id"] for r in only_reqs],
            "approx_bytes": bytes_total,
        }
        (BATCH_REGISTRY / f"{batch.id}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2)
        )
        print(f"  → batch_id {batch.id}  ({batch.processing_status})")

    return batch_ids


def list_batches(client: anthropic.Anthropic) -> None:
    if not BATCH_REGISTRY.exists():
        print("No batches submitted from this machine yet "
              f"({BATCH_REGISTRY}).")
        return
    records = sorted(BATCH_REGISTRY.glob("*.json"))
    if not records:
        print("Registry empty.")
        return
    for rec_path in records:
        rec = json.loads(rec_path.read_text())
        bid = rec["batch_id"]
        try:
            batch = client.messages.batches.retrieve(bid)
            counts = batch.request_counts
            print(f"  {bid}  status={batch.processing_status:<10}  "
                  f"succ={counts.succeeded:<4} "
                  f"err={counts.errored:<3} proc={counts.processing:<4} "
                  f"({rec['request_count']} total, model={rec['model']})")
        except Exception as e:
            print(f"  {bid}  [error fetching: {e}]")


def fetch_batch(client: anthropic.Anthropic, batch_id: str, model: str) -> int:
    """Stream results for a finished batch; write one JSON per page."""
    batch = client.messages.batches.retrieve(batch_id)
    if batch.processing_status != "ended":
        print(f"Batch {batch_id} not finished yet: "
              f"status={batch.processing_status}")
        return 0
    suffix = "" if model == DEFAULT_MODEL else f"_{model.split('-')[1]}"
    n_ok = n_err = 0
    for result in client.messages.batches.results(batch_id):
        cid = result.custom_id  # e.g. "page_02_603"
        # Parse "page_VV_LL" → vol, leaf
        _, vol, leaf_s = cid.split("_")
        leaf = int(leaf_s)
        if result.result.type != "succeeded":
            n_err += 1
            print(f"  [fail] {cid}: {result.result.type}")
            continue
        msg = result.result.message
        payload = None
        for block in msg.content:
            if block.type == "tool_use" and block.name == "record_page_entries":
                payload = block.input
                break
        if payload is None:
            n_err += 1
            print(f"  [fail] {cid}: no tool_use block")
            continue
        # A few pages have no Balearic entries — Claude calls the tool
        # with an empty input ({}). Treat that as "0 entries on page".
        out = {
            "vol": vol,
            "leaf": leaf,
            "page_printed": get_page_printed(vol, leaf),
            "model": model,
            "batch_id": batch_id,
            "usage": {
                "input_tokens": msg.usage.input_tokens,
                "output_tokens": msg.usage.output_tokens,
            },
            "entries": payload.get("entries", []),
        }
        out_path = OUT_DIR / f"page_{vol}_{leaf}{suffix}.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        n_ok += 1
    print(f"\n[batch {batch_id}] {n_ok} written, {n_err} failed")
    return n_ok


def extract_page(
    client: anthropic.Anthropic,
    vol: str,
    leaf: int,
    model: str = DEFAULT_MODEL,
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
        model=model,
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
        "model": model,
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
    sub.add_argument("--batch-submit", action="store_true",
                     help="build + submit Batch API job(s) for unprocessed pages")
    sub.add_argument("--batch-status", action="store_true",
                     help="list submitted batches and their progress")
    sub.add_argument("--batch-fetch", metavar="BATCH_ID",
                     help="fetch finished batch results (BATCH_ID or 'all')")
    ap.add_argument("--overwrite", action="store_true",
                    help="re-extract pages even if their JSON already exists")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    choices=list(MODEL_PRICING),
                    help=f"model to use (default {DEFAULT_MODEL})")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set in environment.")
    client = anthropic.Anthropic()

    # Batch-mode branches return early — they don't consume the
    # sync-mode loop below.
    if args.batch_submit:
        ids = submit_batches(client, args.model, args.overwrite)
        if ids:
            print("\nSubmitted batch IDs (saved to data/vision/.batches/):")
            for bid in ids:
                print(f"  {bid}")
            print("\nCheck progress with:  python scripts/extract_vision.py --batch-status")
            print("Fetch when ended with: python scripts/extract_vision.py --batch-fetch all")
        return
    if args.batch_status:
        list_batches(client)
        return
    if args.batch_fetch:
        if args.batch_fetch == "all":
            if not BATCH_REGISTRY.exists():
                sys.exit("No batches in registry.")
            ids = [json.loads(p.read_text())["batch_id"]
                   for p in sorted(BATCH_REGISTRY.glob("*.json"))]
        else:
            ids = [args.batch_fetch]
        total = 0
        for bid in ids:
            total += fetch_batch(client, bid, args.model)
        print(f"\nDone. {total} page JSON file(s) written to {OUT_DIR}/")
        return

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
    # When comparing models, put the model name in the filename so runs
    # don't overwrite each other.
    suffix = "" if args.model == DEFAULT_MODEL else f"_{args.model.split('-')[1]}"
    for vol, leaf in pairs:
        out_path = OUT_DIR / f"page_{vol}_{leaf}{suffix}.json"
        if out_path.exists() and not args.overwrite:
            print(f"  [skip] {out_path.name} already exists")
            continue
        try:
            print(f"  [GET]  tom{vol} leaf{leaf} ({args.model})...", flush=True)
            result = extract_page(client, vol, leaf, model=args.model)
        except FileNotFoundError as e:
            print(f"  [skip] {e}")
            continue
        except Exception as e:
            print(f"  [fail] tom{vol} leaf{leaf}: {e}", file=sys.stderr)
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        ti = result["usage"]["input_tokens"]
        to = result["usage"]["output_tokens"]
        total_in += ti
        total_out += to
        print(f"  [ok]   {out_path.name}: {len(result['entries'])} entries "
              f"(in={ti} out={to} toks)")
        # Polite small pause to avoid bursts
        time.sleep(0.2)

    if total_in or total_out:
        rate = MODEL_PRICING[args.model]
        cost = total_in / 1_000_000 * rate["in"] + total_out / 1_000_000 * rate["out"]
        print()
        print(f"Tokens: in={total_in:,}  out={total_out:,}  ({args.model})")
        print(f"Estimated cost (non-batch): ${cost:.4f}")
        print(f"Projected for 684 pages:    ${cost / max(1, len(pairs)) * 684:.2f}")


if __name__ == "__main__":
    main()
