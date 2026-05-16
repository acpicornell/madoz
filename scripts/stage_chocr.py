"""Dump the chocr plaintext for a list of (vol, leaf) targets.

Used to feed Claude (in-conversation, no API) the same chocr input that
extract_text.py would send to the Sonnet API. For each target writes:
  data/text/_chocr/page_<vol>_<leaf>.txt

The .txt has two sections: TARGET LEAF and NEXT LEAF (continuation
context).

Run:
  python scripts/stage_chocr.py 02:603 08:146 09:379 11:226 13:157
  python scripts/stage_chocr.py --from-db --limit 5   (auto-pick from chocr_entries)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
CHOCR_DIR = PROJECT / "data" / "chocr"
OUT_DIR = PROJECT / "data" / "text" / "_chocr"

sys.path.insert(0, str(PROJECT / "scripts"))
from extract_text import build_leaf_text, pick_window  # type: ignore


def get_page_printed(vol: str, leaf: int) -> str | None:
    con = duckdb.connect(str(DB), read_only=True)
    row = con.execute(
        "SELECT page_printed FROM chocr_entries WHERE vol = ? AND leaf = ? LIMIT 1",
        [vol, leaf],
    ).fetchone()
    return row[0] if row else None


def get_chocr_titles(vol: str, leaf: int) -> list[tuple[str, str]]:
    con = duckdb.connect(str(DB), read_only=True)
    rows = con.execute(
        """SELECT title, source FROM chocr_entries
           WHERE vol = ? AND leaf = ? ORDER BY title""",
        [vol, leaf],
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def stage(vol: str, leaf: int, window: int | None = None) -> Path:
    chocr_path = CHOCR_DIR / f"tomo{vol}.html.gz"
    if not chocr_path.exists():
        raise FileNotFoundError(chocr_path)
    titles = get_chocr_titles(vol, leaf)
    pp = get_page_printed(vol, leaf)
    if window is None:
        window = pick_window([{"title": t, "source": s} for t, s in titles])
    leaf_text, continuations = build_leaf_text(chocr_path, leaf, window=window)

    header = (
        f"# tom{vol} leaf {leaf}  printed page {pp or '?'}  window={window}\n"
        f"# chocr-indexed Balearic entries on this leaf ({len(titles)}):\n"
    )
    for t, src in titles:
        header += f"#   - {t}  ({src})\n"

    body = "=== TARGET LEAF ===\n" + leaf_text
    for i, cont in enumerate(continuations, start=1):
        body += f"\n\n=== CONTINUATION LEAF +{i} ===\n{cont}"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"page_{vol}_{leaf}.txt"
    out.write_text(header + "\n" + body)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("targets", nargs="*",
                    help="VOL:LEAF pairs (e.g. 02:603 08:146)")
    ap.add_argument("--from-db", action="store_true",
                    help="auto-pick leaves from chocr_entries (DISTINCT vol,leaf)")
    ap.add_argument("--limit", type=int, default=10,
                    help="--from-db: how many to stage (default 10)")
    ap.add_argument("--window", type=int, default=None,
                    help="force window size (default: auto, 4 for mega-entries, 2 otherwise)")
    args = ap.parse_args()

    if args.from_db:
        con = duckdb.connect(str(DB), read_only=True)
        rows = con.execute(
            "SELECT DISTINCT vol, leaf FROM chocr_entries ORDER BY vol, leaf LIMIT ?",
            [args.limit],
        ).fetchall()
        pairs = [(r[0], r[1]) for r in rows]
    else:
        pairs = []
        for t in args.targets:
            vol, leaf = t.split(":")
            pairs.append((vol.zfill(2), int(leaf)))

    if not pairs:
        sys.exit("No targets specified. Pass VOL:LEAF pairs or --from-db.")

    for vol, leaf in pairs:
        try:
            out = stage(vol, leaf, window=args.window)
            size = out.stat().st_size
            print(f"  [ok] {out.relative_to(PROJECT)}  ({size:,} bytes)")
        except Exception as e:
            print(f"  [fail] tom{vol} leaf{leaf}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
