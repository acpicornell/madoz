"""Infer place_type from description text when the field is NULL.

The LLM extractor occasionally left place_type empty even when the
description's leading word ("predio en la isla de Mallorca …") makes the
type unambiguous. This script fills the obvious cases — predio, villa,
aldea, alquería, cala, granja, … — leaving genuine ambiguities alone.

Run AFTER load_text.py. Idempotent.

  python scripts/infer_place_types.py            # dry run
  python scripts/infer_place_types.py --apply    # commit
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

# Each rule fires when its pattern is found in the description's first
# ~80 chars. Order matters: first match wins.
HEAD_LEN = 80
RULES: list[tuple[re.Pattern, str]] = [
    # predio — by far the biggest category (~70 rows)
    (re.compile(r"(?:^|[^a-z])(?:p[ré]?[eé]dios?|piedio|prédio)(?:[^a-z]|$)", re.IGNORECASE), "predio"),
    # ciudad antigua — "c. desaparecida" / "c. antigua"
    (re.compile(r"^[^\w]*c\.\s*(?:desaparecida|antigua)\b", re.IGNORECASE), "ciudad antigua"),
    # villa — "v.", "v.,", "Vi" (OCR garble of V.), "villa"
    (re.compile(r"^[^\w]*(?:v[íi]?\.[\s,]|vi\s|villa\b)", re.IGNORECASE), "villa"),
    # aldea
    (re.compile(r"^[^\w]*(?:pequeña\s+)?ald[\.,]?\s", re.IGNORECASE), "aldea"),
    (re.compile(r"\baldea\b", re.IGNORECASE), "aldea"),
    # alquería
    (re.compile(r"\balq[\.,]?\s", re.IGNORECASE), "alquería"),
    (re.compile(r"\balquer[íi]a\b", re.IGNORECASE), "alquería"),
    # cala
    (re.compile(r"^[^\w]*cala\b", re.IGNORECASE), "cala"),
    # granja
    (re.compile(r"\bgranja\b", re.IGNORECASE), "granja"),
    # población desaparecida
    (re.compile(r"\bpobl(?:aci[oó]n)?\.?\s+desaparecida\b", re.IGNORECASE), "población desaparecida"),
    # cueva
    (re.compile(r"^[^\w]*cueva\b", re.IGNORECASE), "cueva"),
    # cuartón
    (re.compile(r"\bcuart[oó]n\b", re.IGNORECASE), "cuartón"),
    # isleta
    (re.compile(r"^[^\w]*isleta\b", re.IGNORECASE), "isleta"),
    # casa de campo
    (re.compile(r"\bcasa\s+de\s+campo\b", re.IGNORECASE), "casa de campo"),
    # denominación histórica (OPHIüSA-style — apostrophe variants tolerated)
    (re.compile(r"nombre\s+que\s+se\s+di[oó].{0,4}en\s+la\s+antig", re.IGNORECASE | re.DOTALL), "denominación histórica"),
    # "con esta denominación se conocen" / "bajo esta denominación se conocen"
    # → describes a cluster of predios
    (re.compile(r"\b(?:con|bajo)\s+est[ae]?\s+(?:denominaci[oó]n|nombre)\s+se\s+conocen", re.IGNORECASE), "predio"),
    # "campos antiguamente del predio" — PEDRET DE BOCAR (OCR may glue
    # "del predio" + capitalized owner name with no space)
    (re.compile(r"\bcampos\s+antiguamente\s+del\s+predio", re.IGNORECASE), "predio"),
]


def infer(desc: str | None) -> str | None:
    if not desc:
        return None
    head = desc[:HEAD_LEN]
    for pat, pt in RULES:
        if pat.search(head):
            return pt
    return None


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB), read_only=not apply)

    rows = con.execute(
        "SELECT id, title, description, source_file, confidence "
        "FROM text_entries WHERE place_type IS NULL"
    ).fetchall()

    plan: list[tuple[int, str, str, str, str]] = []
    skipped: list[tuple[int, str, str]] = []
    for tid, title, desc, src, conf in rows:
        pt = infer(desc)
        if pt:
            plan.append((tid, title, pt, src, conf))
        else:
            skipped.append((tid, title, (desc or "")[:60]))

    print(f"{len(plan)} place_type inferences pending (out of {len(rows)} NULL rows).")
    from collections import Counter
    by_type = Counter(p[2] for p in plan)
    for pt, n in by_type.most_common():
        print(f"  {n:4}  → {pt!r}")
    print(f"\n{len(skipped)} rows left unclassified (description is ambiguous):")
    for tid, title, snip in skipped[:15]:
        print(f"  id={tid:5} {title[:32]:<32}  {snip}")
    if len(skipped) > 15:
        print(f"  ... +{len(skipped)-15} more")

    if not apply:
        if plan:
            print("\nDRY RUN — pass --apply to commit.")
        return

    for tid, title, pt, src, _conf in plan:
        path = PROJECT / src
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for e in data.get("entries", []):
                if e.get("title") == title and not e.get("place_type"):
                    e["place_type"] = pt
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        con.execute(
            "UPDATE text_entries SET place_type=? WHERE id=?",
            [pt, tid],
        )
    print(f"\nFilled {len(plan)} rows.")


if __name__ == "__main__":
    main()
