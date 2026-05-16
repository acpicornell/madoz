"""Repair OCR-glued tokens inside text_entries.description.

The chocr OCR loses spaces around short Spanish function words ("de",
"del", "en la", "y") and after abbreviations ("part.jud", "térm. y").
This script does a set of targeted, conservative substitutions on every
description, both in the DB and in the data/text/*.json sources, so a
re-run of load_text.py preserves the fix.

Each rule is whitespace-only (it inserts a missing space) and idempotent.
None deletes or substitutes content — re-running on already-clean text
is a no-op.

  python scripts/clean_descriptions.py            # dry run, report counts
  python scripts/clean_descriptions.py --apply    # commit
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

# (regex, replacement, human label). Apply in this order. Each rule
# only adds whitespace inside the glued sequence so they compose
# cleanly and are idempotent.
RULES: list[tuple[re.Pattern, str, str]] = [
    # Multi-token combos first — they fold into the simpler rules afterwards.
    (re.compile(r"\bdelav\."), "de la v.", "delav. → de la v."),
    (re.compile(r"\blav\.(?=\s|$)"), "la v.", "lav. → la v."),
    (re.compile(r"\bdelas\b"), "de las", "delas → de las"),
    (re.compile(r"\bdelos\b"), "de los", "delos → de los"),
    (re.compile(r"\benla\b"), "en la", "enla → en la"),
    # Glued abbreviations
    (re.compile(r"\bpart\.jud\b"), "part. jud.", "part.jud → part. jud."),
    (re.compile(r"\byjurisd\b"), "y jurisd", "yjurisd → y jurisd"),
    (re.compile(r"\bytruchas\b"), "y truchas", "ytruchas → y truchas"),
    # "Baleares,part." etc. — comma directly hitting a letter
    (re.compile(r"Baleares,(?=[A-Za-z])"), "Baleares, ", "Baleares,X → Baleares, X"),
    # ",c." style abbreviation glue (",c. g." → ", c. g.")
    (re.compile(r",(?=[a-z]\.)"), ", ", ",x. → , x."),
    # "de" / "del" + capital word — the big bucket (~180 rows)
    (re.compile(r"\bde(?=[A-ZÁÉÍÓÚÑ][a-záéíóúñ])"), "de ", "deX → de X"),
    (re.compile(r"\bdel(?=[A-ZÁÉÍÓÚÑ][a-záéíóúñ])"), "del ", "delX → del X"),
    # Known Madoz abbreviations + lowercase word (no space after period).
    # Whitelisted explicitly so we never split a legit ordinal ("1.er")
    # or initial ("S.M.") — only these abbreviations are split.
    (re.compile(r"\b(prov|part|t[eé]rm|jurisd|aud|dióc|c|v|f|cab)\.(?=[a-záéíóúñ])"), r"\1. ", "<abbr>.lower → <abbr>. lower"),
]


def clean(s: str) -> tuple[str, dict[str, int]]:
    """Return (cleaned_text, per-rule replacement counts)."""
    counts: dict[str, int] = {}
    for pat, repl, label in RULES:
        new_s, n = pat.subn(repl, s)
        if n:
            counts[label] = n
        s = new_s
    return s, counts


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    rows = con.execute(
        "SELECT id, title, description, source_file FROM text_entries"
    ).fetchall()

    plan: list[tuple[int, str, str, str, dict]] = []
    for tid, title, desc, src in rows:
        if not desc:
            continue
        cleaned, counts = clean(desc)
        if cleaned != desc:
            plan.append((tid, title, desc, cleaned, src))

    # Aggregate per-rule counts across the whole corpus.
    from collections import Counter
    agg = Counter()
    for _, _, desc, _, _ in plan:
        _, counts = clean(desc)
        for label, n in counts.items():
            agg[label] += n

    print(f"{len(plan)} descriptions would change ({sum(agg.values())} edits total):")
    for label, n in agg.most_common():
        print(f"  {n:4}× {label}")

    # Show a few diffs so we can eyeball the changes.
    print("\n--- sample diffs ---")
    for tid, title, before, after, _ in plan[:5]:
        # Mark first differing region
        i = next((k for k, (a, b) in enumerate(zip(before, after)) if a != b), 0)
        print(f"  id={tid} {title}")
        print(f"     before: ...{before[max(0,i-25):i+45]!r}...")
        print(f"     after : ...{after [max(0,i-25):i+45]!r}...")

    if not apply:
        if plan:
            print("\nDRY RUN — pass --apply to commit.")
        return

    # Patch JSON source files (so a future load_text.py keeps the fix)
    # and the DB rows. Both in the same loop, batched per source file.
    by_src: dict[str, list[tuple[int, str, str]]] = {}
    for tid, title, _before, after, src in plan:
        by_src.setdefault(src, []).append((tid, title, after))

    for src, items in by_src.items():
        path = PROJECT / src
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            wanted = {t for _, t, _ in items}
            cleaned_map = {t: a for _, t, a in items}
            changed = False
            for e in data.get("entries", []):
                if e.get("title") in wanted and e.get("description"):
                    new_desc, _ = clean(e["description"])
                    if new_desc != e["description"]:
                        e["description"] = new_desc
                        changed = True
            if changed:
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        for tid, _, after in items:
            con.execute(
                "UPDATE text_entries SET description=? WHERE id=?",
                [after, tid],
            )

    print(f"\nCleaned {len(plan)} descriptions across {len(by_src)} source files.")


if __name__ == "__main__":
    main()
