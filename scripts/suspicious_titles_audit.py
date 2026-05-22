#!/usr/bin/env python3
"""Audit extracted article titles for OCR damage and other anomalies.

For each title in ``text_entries``, two signals are checked:

1.  **Pattern heuristics** — recurring OCR confusions documented in
    this corpus:
      * ``BIM`` / ``BUM`` / ``BUNI`` prefixes where Mallorcan toponymy
        expects ``BINI`` (NI→M, NI→U+M, NI→U OCR confusion).
      * Letter ``K`` in a Romance toponym, almost always a misread
        ``R`` (Mallorcan/Menorquin Catalan does not use K).
      * Diaeresis on ``Ü`` outside the canonical Catalan ``üe`` /
        ``üi`` digraphs.
      * Diaeresis on ``Ï`` (very rare in Catalan/Castilian toponyms).
      * Stray lowercase letters inside an otherwise-uppercase headword
        (``BAÑO lBUFAR`` style).
      * Stray punctuation characters (``[]{}|\\^"§``).
      * Uncommon consonant clusters (``KK``, ``WW``, ``QQ``, ``VV``).
      * Length sanity (≤ 2 characters).

2.  **Curated-mirror cross-check** — if a flagged title fuzzy-matches
    (WRatio ≥ 88) something in ``madoz_entries`` (the diccionariomadoz
    .com scrape), it is downgraded to *archaic-spelling note*: the
    OCR-looking quirk is real Madoz typography, not scanner damage
    (e.g. Madoz prints *BENISALEM* where the modern form is
    *BINISSALEM*, so ``BIM-``-flagged titles that match a curated
    ``BIN-``/``BEN-`` entry at high score should not be re-fixed).

This is the heuristic-only Madoz analogue of minano's
``suspicious_titles_audit.py``. The NGIB-fuzzy half of minano's
audit is deferred to a later wave that adds the Balearic gazetteer
to this project.

Usage:
    python scripts/suspicious_titles_audit.py
    python scripts/suspicious_titles_audit.py --threshold 85
    python scripts/suspicious_titles_audit.py --vol 11
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path

import duckdb
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "db" / "madoz.duckdb"


def normalize(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Suffixes that decorate the title but are not part of the toponym
# (administrative annotations and editorial markers).
SUFFIX_RE = re.compile(
    r"\s*\((?:adicio?n[es]?|isla|isla de|Isla|Term|T[eé]rmino|Desp|"
    r"part\.?\s*jud|estad[ií]sticas?[^)]*|adici[oó]n[^)]*|"
    r"part\.?\s*jud\.?[^)]*)\)\s*$",
    re.IGNORECASE,
)
BRACKET_RE = re.compile(r"\s*\[[^\]]+\]")


def clean_title(raw: str) -> str:
    t = BRACKET_RE.sub("", raw or "")
    t = SUFFIX_RE.sub("", t)
    return t.strip()


def heuristic_flags(title: str) -> list[str]:
    flags = []
    core = clean_title(title)
    upper = core.upper()

    # BIM / BUM / BUNI prefix where Mallorcan toponymy starts with BINI-.
    if re.match(r"^BIM[A-ZÁÉÍÓÚÑÜ]", upper):
        flags.append("BIM- prefix (probably BINI-, NI→M OCR confusion)")
    if re.match(r"^BUM[A-ZÁÉÍÓÚÑÜ]", upper):
        flags.append("BUM- prefix (probably BINI-, NI→U+M)")
    if re.match(r"^BUNI[A-ZÁÉÍÓÚÑÜ]", upper):
        flags.append("BUNI- prefix (probably BINI-, NI→U)")

    # K — practically does not appear in Catalan or Castilian toponyms.
    if re.search(r"K", upper):
        flags.append("contains K (probably R, OCR R→K)")

    # W — extremely rare.
    if re.search(r"W", upper):
        flags.append("contains W (unusual for Balearic toponym)")

    # Ü outside the Catalan «üe» / «üi» digraphs.
    if "Ü" in upper and not re.search(r"Ü[EI]", upper):
        flags.append("Ü outside «üe/üi» (probable OCR umlaut artefact)")

    # Ï — even rarer; only valid between vowels in Catalan.
    if "Ï" in upper:
        flags.append("Ï present (uncommon in toponyms)")

    # Stray lowercase letters inside a predominantly-uppercase word.
    # Skip short words and Titlecase-by-design (Palma, Ibiza in
    # parenthetical "(isla de Palma)" etc.). Only flag the LEAD word
    # because Madoz titles frequently carry a fully lowercase
    # parenthetical specifier that is legitimate ("(cala del)").
    lead = core.split()[0] if core.split() else ""
    if len(lead) >= 4:
        uppers = sum(1 for c in lead if c.isupper())
        lowers = sum(1 for c in lead if c.islower())
        if uppers >= 2 and lowers >= 1 and lowers <= 2:
            flags.append(f"stray lowercase in lead {lead!r} (probable OCR)")

    # Stray punctuation often points at OCR noise.
    if re.search(r"[\[\]{}|\\^\"§]", core):
        flags.append("stray punctuation")

    # Uncommon consonant clusters (Catalan/Castilian basically never
    # form these); the legitimate ll/ny/tx/rr/ss digraphs are fine.
    if re.search(r"KK|WW|QQ|GG[A-Z]|FF[BCDFGHJKLMNPQRSTVWXZ]|VV", upper):
        flags.append("uncommon consonant cluster")

    # Length sanity — single- or double-character titles are almost
    # always garbage (or non-toponym admin abbreviations).
    if len(core) <= 2:
        flags.append("title too short")

    return flags


def load_titles(vol_filter: str | None) -> list[dict]:
    """Pull (vol, leaf, title, island) tuples from text_entries."""
    if not DB.exists():
        sys.exit(f"Missing {DB}. Build the database first.")
    con = duckdb.connect(str(DB), read_only=True)
    sql = (
        "SELECT vol, leaf, title, island FROM text_entries "
        "WHERE title IS NOT NULL"
    )
    if vol_filter:
        sql += f" AND vol = '{vol_filter.zfill(2)}'"
    sql += " ORDER BY vol, leaf, title"
    return [
        {"vol": r[0], "leaf": r[1], "title": r[2], "island": r[3]}
        for r in con.execute(sql).fetchall()
    ]


def load_curated_pool() -> list[str]:
    """Normalised titles from the curated diccionariomadoz.com mirror."""
    if not DB.exists():
        return []
    con = duckdb.connect(str(DB), read_only=True)
    rows = con.execute("SELECT title FROM madoz_entries WHERE title IS NOT NULL").fetchall()
    return [normalize(r[0]) for r in rows if r[0]]


def load_ngib_pool_by_island() -> dict[str, list[str]]:
    """Normalised toponyms grouped by island from the NGIB gazetteer.

    When the gazetteer parquet is missing this returns an empty dict so
    the audit still runs (heuristic-only mode), which is what happens
    before ``fetch_ngib.py`` + ``build_gazetteer.py`` have been executed.
    """
    gaz = ROOT / "data" / "gazetteer.parquet"
    if not gaz.exists():
        return {}
    con = duckdb.connect(":memory:")
    rows = con.sql(
        f"SELECT normalized, island FROM read_parquet('{gaz}') "
        f"WHERE normalized IS NOT NULL AND normalized <> ''"
    ).fetchall()
    by_isl: dict[str, list[str]] = {}
    for norm, isl in rows:
        by_isl.setdefault(isl or "(unknown)", []).append(norm)
    return by_isl


def best_match(norm_t: str, pool: list[str]) -> tuple[str | None, float]:
    if not pool or not norm_t:
        return None, 0
    nl = len(norm_t)
    best = (None, 0.0)
    for t in pool:
        if not t or len(t) < 0.6 * nl:
            continue
        s = fuzz.WRatio(norm_t, t)
        if s > best[1]:
            best = (t, s)
    return best


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--threshold",
        type=int,
        default=88,
        help="fuzzy match floor for the archaic-spelling downgrade",
    )
    ap.add_argument("--vol", help="restrict to one volume (e.g. 11)")
    args = ap.parse_args()

    titles = load_titles(args.vol)
    curated_pool = load_curated_pool()
    ngib_by_island = load_ngib_pool_by_island()
    findings: list[dict] = []

    for t in titles:
        flags = heuristic_flags(t["title"])
        if not flags:
            continue
        norm_t = normalize(clean_title(t["title"]))
        cur_matched, cur_score = best_match(norm_t, curated_pool)
        # NGIB pool: restrict to the declared island when known so
        # cross-island false matches (Eivissa's es Fornells vs Menorca's
        # Fornells) cannot raise an OCR-damaged title to "archaic".
        ngib_pool = ngib_by_island.get(t.get("island") or "", [])
        if not ngib_pool:  # no island declared → search all
            ngib_pool = [n for ns in ngib_by_island.values() for n in ns]
        ngib_matched, ngib_score = best_match(norm_t, ngib_pool) if ngib_by_island else (None, 0)
        # The stronger of the two pools wins the "is this real Madoz
        # typography?" determination. The curated mirror catches archaic
        # spellings Madoz himself used; NGIB catches modern Catalan
        # toponyms whose 19th-century rendering Madoz preserves.
        if ngib_score >= cur_score:
            matched, score, src = ngib_matched, ngib_score, "ngib"
        else:
            matched, score, src = cur_matched, cur_score, "curated"
        archaic = score >= args.threshold
        findings.append({
            **t,
            "flags": flags,
            "matched": matched,
            "score": score,
            "match_source": src,
            "archaic": archaic,
        })

    probable = [f for f in findings if not f["archaic"]]
    notes = [f for f in findings if f["archaic"]]
    probable.sort(key=lambda f: (f["score"], f["title"]))
    notes.sort(key=lambda f: f["title"])

    print(
        f"=== {len(probable)} probable OCR issue(s), "
        f"{len(notes)} archaic-spelling note(s) "
        f"(curated WRatio < {args.threshold} ⇒ probable) ===\n"
    )
    if probable:
        print("--- PROBABLE OCR / TITLE PROBLEMS ---\n")
        for f in probable:
            flag_str = "; ".join(f["flags"])
            isl = f["island"] or "—"
            print(
                f"  [{f['vol']} leaf {f['leaf']:>3}] "
                f"{f['title']!r:42s}  ({isl})\n"
                f"      flags: {flag_str}\n"
                f"      best match ({f['match_source']}): {f['matched']!r}  "
                f"(score={f['score']:.0f})\n"
            )
    if notes:
        print("--- ARCHAIC SPELLING (strong NGIB/curated match, OCR-looking but real) ---\n")
        for f in notes:
            flag_str = "; ".join(f["flags"])
            print(
                f"  [{f['vol']} leaf {f['leaf']:>3}] "
                f"{f['title']!r:42s}  →  {f['matched']!r} "
                f"({f['match_source']}, score={f['score']:.0f})  [{flag_str}]"
            )
    if not findings:
        print("  clean — no suspicious titles found")


if __name__ == "__main__":
    main()
