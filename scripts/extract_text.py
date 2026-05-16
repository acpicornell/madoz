"""Phase 3 alternative: extract structured Madoz entries from chocr TEXT.

Mirrors `extract_vision.py` but sends Sonnet the per-leaf OCR text from
chocr instead of the page image. This sidesteps the leaf↔IA-image
index mismatch entirely (chocr is keyed by leafNum) and is ~10× cheaper
than Vision. The tradeoff is that statistics (vecinos, almas, contr.)
inherit OCR digit errors that the model can only correct from context.

For each target leaf we send:
  - the full plaintext of the leaf
  - the plaintext of the following leaf as continuation context
    (so cross-page bodies stay intact)

The system prompt asks the model to emit entries whose TITLE appears on
the target leaf only — so the same entry never gets recorded twice.

Modes:
  --page VOL LEAF       extract a single leaf (testing)
  --sample              run on a small diverse curated sample
  --all                 process every leaf with chocr_entries rows

Output: data/text/page_<vol>_<leaf>.json — one file per leaf.

Requires ANTHROPIC_API_KEY in env (loaded from .env).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
from pathlib import Path

import anthropic
import duckdb
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
CHOCR_DIR = PROJECT / "data" / "chocr"
OUT_DIR = PROJECT / "data" / "text"

DEFAULT_MODEL = "claude-sonnet-4-6"
MODEL_PRICING = {
    "claude-opus-4-7":           {"in": 15.00, "out": 75.00},
    "claude-sonnet-4-6":         {"in": 3.00,  "out": 15.00},
    "claude-haiku-4-5-20251001": {"in": 1.00,  "out": 5.00},
}

PAGE_PAT = re.compile(r'class="ocr_page" id="page_(\d+)"')
PAR_OPEN_PAT = re.compile(r'<p class="ocr_par"')
CHAR_PAT = re.compile(r'<span class="ocrx_cinfo"[^>]*>([^<])</span>')


def leaf_paragraphs(chocr_path: Path, target_leaves: set[int]) -> dict[int, list[str]]:
    """Return {leaf: [paragraph_text, ...]} for each target leaf.

    Streams the gzipped chocr file. Stops early once all targets have been
    seen and the current leaf number is past max(targets).
    """
    opener = gzip.open if chocr_path.suffix == ".gz" else open
    out: dict[int, list[str]] = {leaf: [] for leaf in target_leaves}
    current_leaf: int | None = None
    in_par = False
    buf: list[str] = []
    max_leaf = max(target_leaves)
    with opener(chocr_path, "rt", encoding="utf-8") as f:
        for line in f:
            m_page = PAGE_PAT.search(line)
            if m_page:
                if in_par and buf and current_leaf in out:
                    out[current_leaf].append("".join(buf))
                buf = []
                in_par = False
                current_leaf = int(m_page.group(1))
                if current_leaf > max_leaf:
                    break
                continue
            if PAR_OPEN_PAT.search(line):
                if in_par and buf and current_leaf in out:
                    out[current_leaf].append("".join(buf))
                buf = []
                in_par = True
                continue
            if "</p>" in line:
                if in_par and buf and current_leaf in out:
                    out[current_leaf].append("".join(buf))
                buf = []
                in_par = False
                continue
            if in_par and current_leaf in out:
                for cm in CHAR_PAT.finditer(line):
                    buf.append(cm.group(1))
    return out


def normalize_paragraph(text: str) -> str:
    """Collapse whitespace and re-join hyphenated line-end breaks."""
    norm = re.sub(r"\s+", " ", text).strip()
    norm = re.sub(r"(\w)-\s*(\w)", r"\1\2", norm)
    return norm


def build_leaf_text(
    chocr_path: Path, leaf: int, window: int = 2
) -> tuple[str, list[str]]:
    """Return (target_leaf_text, [continuation_leaf_text, ...]).

    window is the total number of leaves to read starting at `leaf`. So:
      window=1 → no continuation (rarely useful)
      window=2 → target + 1 next leaf (default, fine for most entries)
      window=4 → target + 3 next leaves (mega-entries: PALMA, MAHON…)

    Each returned string has paragraphs joined by blank lines. Empty
    leaves (no paragraphs recovered) are dropped from the continuation
    list so its index is not load-bearing.
    """
    if window < 1:
        raise ValueError("window must be ≥1")
    targets = set(range(leaf, leaf + window))
    pars = leaf_paragraphs(chocr_path, targets)
    target_text = "\n\n".join(
        normalize_paragraph(p) for p in pars.get(leaf, []) if p.strip()
    )
    continuations: list[str] = []
    for offset in range(1, window):
        nl_pars = pars.get(leaf + offset, [])
        nl_text = "\n\n".join(normalize_paragraph(p) for p in nl_pars if p.strip())
        if nl_text:
            continuations.append(nl_text)
    return target_text, continuations


# Same schema as Vision: the model must emit a single tool call.
ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": (
                "Cleaned canonical title as printed in Madoz, in ALL CAPS "
                "with any parenthetical specifier preserved literally "
                "(e.g. 'ARTA', 'POQUET (son)', 'MARÍA (Santa)')."
            ),
        },
        "place_type": {
            "type": "string",
            "description": (
                "Spanish lemma for the entry type. Prefer the specific "
                "physical or built feature ('predio', 'alquería', 'villa', "
                "'lugar', 'caserío', 'cala', 'punta', 'cabo', 'sierra', "
                "'valle', 'monte', 'isla', 'islote', 'castillo', 'torre', "
                "'parroquia', 'feligresía', 'arroyo', 'cueva', 'ermita', "
                "'fuente'). Fall back to admin units only when the entry "
                "is itself an admin unit."
            ),
        },
        "island": {
            "type": "string",
            "enum": ["Mallorca", "Menorca", "Ibiza", "Formentera", "Cabrera"],
        },
        "judicial_district": {
            "type": "string",
            "description": (
                "Partido judicial. One of: Inca, Manacor, Palma, Mahón, "
                "Ciudadela, Ibiza. Null if not named."
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
                "Full cleaned transcription of the entry's body, with OCR "
                "errors corrected from context, in modern Spanish "
                "orthography. Preserve abbreviations as Madoz wrote them "
                "('prov.', 'aud. terr.', 'c. g.', 'part. jud.', 'térm.', "
                "'felig.', 'V.' for véase). Keep punctuation."
            ),
        },
        "stats": {
            "type": "object",
            "description": (
                "Statistics block at the end of the entry, if any. Include "
                "only the fields the source actually shows; use integers "
                "for counts. Common keys: 'casas', 'vecinos', 'almas', "
                "'habitantes', 'contribucion', 'riqueza_imponible'. "
                "Stats are OCR-derived: if a number looks implausible "
                "(zero almas with many vecinos, etc.) flag confidence "
                "'low' rather than guessing."
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
                "Your confidence in the structured fields, given that the "
                "INPUT IS OCR (not the facsimile). 'low' when OCR was so "
                "garbled key fields had to be guessed; 'medium' for "
                "moderate ambiguity especially in numeric stats; 'high' "
                "for clean, unambiguous extraction."
            ),
        },
    },
    "required": ["title", "description", "confidence"],
    "additionalProperties": False,
}

TOOL = {
    "name": "record_leaf_entries",
    "description": (
        "Record every Balearic Madoz dictionary entry whose TITLE appears "
        "on the target leaf. One call, with the full list."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "items": ENTRY_SCHEMA,
                "description": "All Balearic entries titled on the target leaf.",
            },
        },
        "required": ["entries"],
    },
}

SYSTEM_PROMPT = """\
You extract structured records from OCR'd text of Pascual Madoz's
"Diccionario geográfico-estadístico-histórico de España y sus posesiones
de ultramar" (Madrid, 1845-1850). The OCR is from a 19th-century
two-column facsimile and is noisy: expect mangled digits (1↔i↔l, 0↔o,
5↔s, 8↔b), broken accents, glued or split words, and stray glyphs.

We care ONLY about Balearic entries — those whose geographic context
names Mallorca, Menorca, Ibiza (Iviza), Formentera or Cabrera. Skip
non-Balearic entries that happen to share the leaf.

Each entry begins with a TITLE in caps followed by a separator (':',
';' or sometimes mangled to other punctuation), then a body that usually
follows the pattern: place-type lemma, geographic placement, climate,
population, terrain, agriculture, commerce, ending with 'POBL.: N vec.,
M alm.; CONTR. ...'. Cross-references take the form 'V. NAME' (véase).

You will receive the chocr text for one target leaf plus the next leaf
as continuation context. Emit ONE record per entry whose TITLE is on the
target leaf — including the full body even if it spills into the next
leaf. Do NOT emit entries that are only continuations of an entry titled
on a previous leaf.

When OCR is uncertain, use context, neighboring entries and standard
Madoz abbreviations to repair. For numeric fields (vecinos, almas,
contribución) prefer the OCR digits but if they're clearly broken (e.g.
'1iI3'), set the field to your best guess and mark confidence 'low'.

Call the `record_leaf_entries` tool exactly once with the full list.\
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
    leaf_text: str,
    continuation_texts: list[str],
) -> str:
    titles_hint = "\n".join(
        f"  - {e['title']}  (chocr source: {e['source']})"
        for e in chocr_entries
    )
    sections = [
        f"Volume tom {vol}, target leaf {leaf}, printed page {page_printed or '?'}.\n",
        f"Our prior chocr pass identified these {len(chocr_entries)} "
        f"Balearic entries titled on this leaf (titles may carry OCR "
        f"noise; trust the text below):\n{titles_hint}\n",
        "=== TARGET LEAF (chocr text) ===\n" + leaf_text,
    ]
    for i, cont in enumerate(continuation_texts, start=1):
        sections.append(
            f"\n=== CONTINUATION LEAF +{i} (do NOT emit entries titled "
            f"here) ===\n{cont}"
        )
    sections.append(
        "\nExtract every Balearic entry whose TITLE appears on the TARGET "
        "leaf. Include the full body even if it spills into the "
        "continuation leaves."
    )
    return "\n".join(sections)


MEGA_TITLES = {
    "PALMA", "MAHON", "MAHÓN", "IBIZA", "IVIZA",
    "MANACOR", "ALCUDIA", "INCA", "CIUDADELA", "FELANITX",
}


def pick_window(chocr_entries: list[dict]) -> int:
    """Return the chocr-text window size (in leaves) for a target.

    Mega-entries (PALMA, MAHON, IBIZA, …) routinely run 3-5 leaves; the
    default 2-leaf window cuts them off before POBL/CONTR. If any entry
    title on the leaf matches a known mega-lemma, widen to 4. Otherwise
    2 is enough.
    """
    for e in chocr_entries:
        base = e["title"].split("(")[0].strip().upper()
        if base in MEGA_TITLES:
            return 4
    return 2


def extract_leaf(
    client: anthropic.Anthropic,
    vol: str,
    leaf: int,
    model: str = DEFAULT_MODEL,
) -> dict:
    chocr_path = CHOCR_DIR / f"tomo{vol}.html.gz"
    if not chocr_path.exists():
        raise FileNotFoundError(f"chocr missing: {chocr_path}")

    chocr_entries = load_chocr_entries_for(vol, leaf)
    page_printed = get_page_printed(vol, leaf)
    window = pick_window(chocr_entries)
    leaf_text, continuations = build_leaf_text(chocr_path, leaf, window=window)
    user_text = build_user_message(
        vol, leaf, page_printed, chocr_entries, leaf_text, continuations
    )

    msg = client.messages.create(
        model=model,
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "record_leaf_entries"},
        messages=[{"role": "user", "content": user_text}],
    )

    payload = None
    for block in msg.content:
        if block.type == "tool_use" and block.name == "record_leaf_entries":
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
        "window": window,
        "usage": {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
        },
        "entries": payload["entries"],
        "chocr_text_leaf": leaf_text,
        "chocr_text_continuations": continuations,
    }


def write_result(result: dict, suffix: str = "") -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"page_{result['vol']}_{result['leaf']}{suffix}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_mutually_exclusive_group(required=True)
    sub.add_argument("--page", nargs=2, metavar=("VOL", "LEAF"),
                     help="extract a single leaf (e.g. --page 02 603)")
    sub.add_argument("--sample", action="store_true",
                     help="run on a curated diverse sample")
    sub.add_argument("--all", action="store_true",
                     help="process every leaf in chocr_entries not yet done")
    ap.add_argument("--overwrite", action="store_true",
                    help="re-extract leaves even if their JSON already exists")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    choices=list(MODEL_PRICING),
                    help=f"model to use (default {DEFAULT_MODEL})")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set in environment.")
    client = anthropic.Anthropic()

    if args.page:
        vol, leaf = args.page[0], int(args.page[1])
        pairs = [(vol, leaf)]
    elif args.sample:
        # Reuse the same anchor pages as the vision sampler for parity.
        from extract_vision import pick_sample  # type: ignore
        pairs = pick_sample()
        print(f"Sample size: {len(pairs)}")
    else:
        con = duckdb.connect(str(DB), read_only=True)
        pairs = [(r[0], r[1]) for r in con.execute(
            "SELECT DISTINCT vol, leaf FROM chocr_entries ORDER BY vol, leaf"
        ).fetchall()]
        print(f"All leaves: {len(pairs)}")

    suffix = "" if args.model == DEFAULT_MODEL else f"_{args.model.split('-')[1]}"
    total_in = total_out = 0
    for vol, leaf in pairs:
        out_path = OUT_DIR / f"page_{vol}_{leaf}{suffix}.json"
        if out_path.exists() and not args.overwrite:
            print(f"  [skip] {out_path.name} already exists")
            continue
        try:
            print(f"  [GET]  tom{vol} leaf{leaf} ({args.model})...", flush=True)
            result = extract_leaf(client, vol, leaf, model=args.model)
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
        time.sleep(0.2)

    if total_in or total_out:
        rate = MODEL_PRICING[args.model]
        cost = total_in / 1_000_000 * rate["in"] + total_out / 1_000_000 * rate["out"]
        print()
        print(f"Tokens: in={total_in:,}  out={total_out:,}  ({args.model})")
        print(f"Estimated cost (non-batch): ${cost:.4f}")
        print(f"Projected for 684 leaves:   ${cost / max(1, len(pairs)) * 684:.2f}")


if __name__ == "__main__":
    main()
