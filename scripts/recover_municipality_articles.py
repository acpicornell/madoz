"""Re-extract truncated municipality / partido-judicial articles by
feeding the chocr text of the relevant leaves to Claude Sonnet again
with a focused prompt.

The original Claude-text pass went leaf-by-leaf and capped output
per call, so multi-leaf mega-articles (PALMA, IBIZA, MAHON, ALCUDIA,
…) ended up with only the first paragraph or two. This pass:

  1. For each candidate text_entries.id, reads the chocr window
     file (which already includes ±4 adjacent leaves).
  2. Asks Claude to identify and extract the full body of the
     article whose title matches our row, fixing OCR glue but
     keeping Madoz's abbreviations and prose style. Stats tables
     are explicitly skipped (chocr OCR mangles them anyway).
  3. Updates the description in the DB and the source JSON.

Idempotent: only touches an entry if its current description is
significantly shorter than what diccionariomadoz.com has for the
linked madoz_entries row.

  python scripts/recover_municipality_articles.py            # dry run
  python scripts/recover_municipality_articles.py --apply
  python scripts/recover_municipality_articles.py --ids 8473 8474   # subset
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

import anthropic
import duckdb
from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT / ".env")
DB = PROJECT / "db" / "madoz.duckdb"
CHOCR_DIR = PROJECT / "data" / "text" / "_chocr"

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
You re-extract structured article bodies from chocr text of Pascual
Madoz's "Diccionario geográfico-estadístico-histórico de España"
(Madrid 1845-1850).

The chocr text we hand you covers a target leaf and a few adjacent
ones, and contains MANY articles in alphabetical order — peninsular
and Balearic — interspersed with statistics tables that the OCR
mangles badly.

We tell you the specific Balearic article we care about (title +
context). Your job: locate it in the chocr text and produce the
FULL CLEAN article body, preserving Madoz's style:

  - Keep his abbreviations exactly (prov., aud. terr., c. g.,
    part. jud., térm., dióc., V. for véase, etc.)
  - Fix obvious OCR glue ("deBaleares" → "de Baleares", "v.dePalma"
    → "v. de Palma")
  - Concatenate paragraphs that continue across leaf boundaries
  - Use section headers as Madoz prints them: SIT. Y CLIMA,
    INTERIOR DE LA POBLACIÓN, TÉRMINO, CALIDAD Y CIRCUNSTANCIAS
    DEL TERRENO, PRODUCCIONES, INDUSTRIA, COMERCIO, POBLACIÓN,
    HISTORIA, …
  - When the article includes stats tables, do NOT transcribe the
    numbers (they're chocr garbage); instead insert the bracketed
    placeholder:
      [Madoz inclou aquí una taula d'estadístiques (...) que el
       chocr OCR no llegeix de manera coherent; vegeu el facsímil
       per als valors.]
  - Skip non-Balearic homonyms (e.g. SALAS Orense, PALMA Tenerife)
    that share the page with our target
  - Skip the article's own title-line — start from the body

If the article continues beyond the chocr window we provided,
finish with the last paragraph available and add:
  [L'article continua als fulls següents del Tom XII / XIII / ...]

Return just the cleaned article body as plain text. No JSON
wrapping, no markdown.
"""


def fetch_targets(con: duckdb.DuckDBPyConnection, ids: list[int] | None):
    sql = """
      SELECT t.id, t.vol, t.leaf, t.page_printed, t.title, t.place_type,
             t.island, t.judicial_district, t.municipality,
             t.description, t.source_file, t.madoz_entry_id,
             length(t.description) AS our_len,
             length(m.content_text) AS their_len
      FROM text_entries t
      LEFT JOIN madoz_entries m ON m.id = t.madoz_entry_id
      WHERE t.place_type IN ('villa', 'ciudad', 'lugar',
                              'partido judicial', 'villa con ayuntamiento')
    """
    params: list = []
    if ids:
        sql += f" AND t.id IN ({','.join('?'*len(ids))})"
        params.extend(ids)
    else:
        # default: truncated mega-articles
        sql += """
          AND m.content_text IS NOT NULL
          AND length(m.content_text) > 2 * length(t.description)
          AND length(m.content_text) >= 1500
        """
    sql += " ORDER BY length(COALESCE(m.content_text,'')) - length(t.description) DESC"
    return con.execute(sql, params).fetchall()


def extract_one(
    client: anthropic.Anthropic,
    row: tuple,
) -> str | None:
    (tid, vol, leaf, page, title, place_type, island, district,
     municipality, old_desc, source_file, _mid, _our_len, _their_len) = row
    chocr_path = CHOCR_DIR / f"page_{vol}_{leaf}.txt"
    if not chocr_path.exists():
        print(f"  [skip] id={tid}: no chocr file {chocr_path.name}")
        return None
    chocr_text = chocr_path.read_text(encoding="utf-8", errors="ignore")

    user_msg = (
        f"Target article: {title!r}\n"
        f"Context: tom {vol}, leaf {leaf}, printed page {page}, "
        f"place_type={place_type}, island={island}, "
        f"judicial_district={district}, municipality={municipality}.\n\n"
        f"Our current (truncated) extraction starts with: "
        f"{(old_desc or '')[:200]!r}\n\n"
        f"Below is the chocr text for the target leaf plus a few "
        f"adjacent leaves. Find and return the full body of the "
        f"target Balearic article.\n\n"
        f"=== chocr text ===\n{chocr_text}"
    )
    msg = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    return text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="actually write the updates back")
    ap.add_argument("--ids", type=int, nargs="+",
                    help="only process these specific text_entries.id")
    ap.add_argument("--limit", type=int,
                    help="cap how many entries to process this run")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set.")
    client = anthropic.Anthropic()

    con = duckdb.connect(str(DB), read_only=not args.apply)
    rows = fetch_targets(con, args.ids)
    if args.limit:
        rows = rows[:args.limit]
    print(f"Found {len(rows)} candidate entries.")
    for r in rows:
        ratio = (r[13] or 0) / max(r[12], 1)
        print(f"  id={r[0]:5} tom{r[1]}/{r[2]:<4} {r[4]!r:<28}  "
              f"our={r[12]:5}  theirs={r[13] or 0:5}  ({ratio:.1f}×)")

    if not rows:
        return
    if not args.apply:
        print("\nDRY RUN — pass --apply to actually call the API and write.")
        return

    total_in = total_out = 0
    for row in rows:
        tid = row[0]
        title = row[4]
        print(f"\n→ id={tid} {title!r} (tom {row[1]}/{row[2]}) — extracting…")
        try:
            new_desc = extract_one(client, row)
        except Exception as e:
            print(f"  [fail] {e}")
            continue
        if not new_desc:
            continue

        # Patch source JSON
        src = PROJECT / row[10]
        if src.exists():
            data = json.loads(src.read_text(encoding="utf-8"))
            for e in data.get("entries", []):
                if e.get("title") == title:
                    e["description"] = new_desc
                    e.setdefault(
                        "note", "Re-extracted from chocr by recover_municipality_articles.py."
                    )
            src.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # Patch DB
        con.execute(
            "UPDATE text_entries SET description=?, note=COALESCE(note, ?) WHERE id=?",
            [new_desc, "Re-extracted from chocr by recover_municipality_articles.py.", tid],
        )
        print(f"  ✓ id={tid}: {len(new_desc)} chars (was {row[12]})")


if __name__ == "__main__":
    main()
