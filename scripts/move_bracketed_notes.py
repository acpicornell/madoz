"""Move editorial bracketed annotations out of ``description`` and into
the ``note`` field where they belong.

The previous LLM extraction left inline `[ ... ]` annotations in the
description body for three things:

1. Editorial OCR comments ("[OCR original lee 'Murcia'; corregido por
   contexto.]", "[Nota: el texto dice 'isla de Mallorca' pero
   geográficamente está en Menorca]", etc.)
2. Table placeholders ("[Madoz inclou aquí una taula d'estadístiques
   (...) que el chocr OCR no llegeix de manera coherent; vegeu el
   facsímil per als valors.]")
3. Continuation markers ("[L'article continua als fulls següents del
   Tom XI...]")

All three are editorial metadata, not part of Madoz's text. This
script moves them to the ``note`` field (appending if a note already
exists, skipping if the same content is already there) and strips them
from the description, cleaning the leftover whitespace.

One special case left alone for manual review: rows where the bracket
is a clarifying gloss for the actual text (e.g. CINIUM has 'Hoy se
llama Sinen [Sineu]' where [Sineu] disambiguates the modern spelling).

  python scripts/move_bracketed_notes.py            # dry run
  python scripts/move_bracketed_notes.py --apply
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"


BRACKET_RX = re.compile(r"\s*\[[^\]]+\]\s*")

# Bracket content that we MOVE to the note. Anything not matching this
# is left in place (CINIUM '[Sineu]' style glosses, future unknown
# patterns).
MOVE_PATTERNS = [
    re.compile(r"^OCR\b", re.IGNORECASE),
    re.compile(r"^L'OCR\b", re.IGNORECASE),
    re.compile(r"^Nota\b", re.IGNORECASE),
    re.compile(r"^Original\b", re.IGNORECASE),
    re.compile(r"^El\s+texto\b", re.IGNORECASE),
    re.compile(r"^Madoz\s+inclou\b", re.IGNORECASE),
    re.compile(r"^L'article\s+continua\b", re.IGNORECASE),
    re.compile(r"^Conocida\b", re.IGNORECASE),    # historical-context note
    re.compile(r"^Comentari\b", re.IGNORECASE),
    re.compile(r"^Antiguamente\b", re.IGNORECASE),
]


def should_move(bracket_content: str) -> bool:
    s = bracket_content.strip()
    return any(p.search(s) for p in MOVE_PATTERNS)


def process(desc: str, existing_note: str | None) -> tuple[str, str | None, list[str]]:
    """Return (new_desc, new_note, moved_brackets)."""
    moved: list[str] = []
    pieces = []
    last = 0
    for m in re.finditer(r"\[[^\]]+\]", desc):
        chunk = desc[m.start() + 1 : m.end() - 1]  # without [ ]
        if should_move(chunk):
            # Output preceding text + a single space; drop the bracket.
            pieces.append(desc[last : m.start()])
            moved.append(chunk)
            last = m.end()
        # else: leave the bracket in place (don't add to moved)
    pieces.append(desc[last:])
    new_desc = "".join(pieces)
    # Whitespace cleanup: collapse runs of spaces, fix spaces around
    # punctuation introduced by bracket removal.
    new_desc = re.sub(r"[ \t]{2,}", " ", new_desc)
    new_desc = re.sub(r"\n[ \t]+\n", "\n\n", new_desc)
    new_desc = re.sub(r"\n{3,}", "\n\n", new_desc)
    new_desc = new_desc.strip()

    new_note = existing_note
    for chunk in moved:
        chunk_clean = chunk.strip()
        if not chunk_clean:
            continue
        if existing_note and chunk_clean in (existing_note or ""):
            continue  # already there
        if new_note:
            new_note = new_note.rstrip() + "\n\n" + chunk_clean
        else:
            new_note = chunk_clean
    return new_desc, new_note, moved


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    rows = con.execute(
        "SELECT id, title, description, note, source_file "
        "FROM text_entries WHERE description LIKE '%[%' ORDER BY id"
    ).fetchall()

    plan = []
    skipped = []
    for tid, title, desc, note, src in rows:
        new_desc, new_note, moved = process(desc, note)
        if not moved:
            skipped.append((tid, title))
            continue
        plan.append({
            "id": tid, "title": title, "src": src,
            "old_desc": desc, "new_desc": new_desc,
            "old_note": note, "new_note": new_note,
            "moved": moved,
        })

    print(f"\n{len(plan)} rows would have bracketed annotations moved "
          f"to the note field.\n")
    for p in plan:
        print(f"  id={p['id']:5} {p['title']!r}")
        for b in p["moved"]:
            short = b if len(b) < 120 else b[:117] + "..."
            print(f"    moved: {short}")
        if p["old_note"] != p["new_note"]:
            old_len = len(p["old_note"] or "")
            new_len = len(p["new_note"] or "")
            print(f"    note: {old_len} -> {new_len} chars")
        print(f"    desc: {len(p['old_desc'])} -> {len(p['new_desc'])} chars")
    if skipped:
        print(f"\n  ({len(skipped)} rows had brackets but none matched the "
              f"move-patterns — left as-is)")
        for tid, t in skipped:
            print(f"    [skip] id={tid} {t!r}")

    if not apply:
        if plan:
            print("\nDRY RUN — pass --apply to commit.")
        return

    # Apply
    json_files_dirty: dict[Path, dict] = {}
    for p in plan:
        path = PROJECT / p["src"]
        if path not in json_files_dirty and path.exists():
            json_files_dirty[path] = json.loads(path.read_text(encoding="utf-8"))
        data = json_files_dirty.get(path)
        if data:
            for e in data.get("entries", []):
                if e.get("title") == p["title"] and e.get("description") == p["old_desc"]:
                    e["description"] = p["new_desc"]
                    e["note"] = p["new_note"]
        con.execute(
            "UPDATE text_entries SET description = ?, note = ? WHERE id = ?",
            [p["new_desc"], p["new_note"], p["id"]],
        )
    for path, data in json_files_dirty.items():
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(f"\n✓ Moved annotations on {len(plan)} rows / "
          f"{len(json_files_dirty)} JSON files.")


if __name__ == "__main__":
    main()
