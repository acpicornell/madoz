#!/usr/bin/env python3
"""Audit chOCR paragraphs for Balearic lemmas buried mid-paragraph.

The main indexer (``scripts/index_volume.py``) matches Madoz lemmas
anchored to the start of a chOCR paragraph. The chOCR sometimes fails
to break between consecutive articles, however, and a Balearic entry
can end up sharing a paragraph with the tail of the preceding (often
peninsular) one. In that situation the indexer is blind to the buried
article.

This is the Madoz-side analogue of minano's ``inner_lemma_audit.py``.
We implement only **Pattern A — tail-merge**: a Balearic article opener
appearing mid-paragraph after the tail of an unrelated entry. The
canonical Madoz signature is ``TITLE: predio en la isla de Mallorca…``
or ``TITLE: v. con ayunt. de la prov. de Baleares…`` — uppercase
lemma, colon separator, then one of the canonical Madoz type markers.

Minano's Pattern B (Suplement chained corrections) does not apply
cleanly to Madoz: Madoz lacks the dense alphabetic Suplement of short
adicions that motivated Pattern B over there. If a pattern of chained
short corrections is later observed in Madoz tomos, that scan can be
added here too.

Candidates whose title is already represented in ``data/text/`` (fuzzy
WRatio ≥ 80) are dropped, so re-running this after a recovery pass
shrinks the result list.

Usage:
    python scripts/inner_lemma_audit.py                # all volumes
    python scripts/inner_lemma_audit.py --vol 08       # one volume
    python scripts/inner_lemma_audit.py --json out.jsonl  # machine-readable
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from index_volume import iter_paragraphs  # noqa: E402

CHOCR = ROOT / "data" / "chocr"
TEXT = ROOT / "data" / "text"


def normalize(s: str) -> str:
    """Lowercase, strip diacritics, collapse to alphanumerics + single spaces.

    Used for fuzzy-matching extracted titles against the known-titles
    pool. Mirrors the shape of minano's gazetteer normalize without
    importing it.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Pattern A — full-article opener buried mid-paragraph (Madoz format).
#
# An uppercase lemma (≥ 3 caps) followed within 40 chars by ``:`` and
# a canonical Madoz body-type marker. The slack between the lemma and
# the colon catches OCR-damaged title bodies (``TUGORES iso):`` where
# the opening paren has been eaten by the OCR, ``GAYA l'A:`` where
# the parenthetical tail is mid-cased).
#
# The lookbehind keeps us from re-matching the start-of-paragraph
# lemma the main indexer already caught.
INNER_LEMMA = re.compile(
    r"(?<![A-ZÁÉÍÓÚÑÜa-záéíóúñü0-9])"
    r"("
        r"[A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ\d\-]{2,18}"   # initial caps run (≥3 caps)
        r"[^:;\n]{0,40}?"                         # title tail up to separator
    r")"
    r"\s*[:;]\s*"
    r"(?:"
        # Full lowercase words common as opener
        r"predio|alquer[íi]a|aldea|villa|lugar|caser[íi]o|cala|cabo|"
        r"punta|monte|sierra|valle|playa|puerto|isla|isleta|islote|"
        r"barrio|cortijo|granja|parroquia|feligres[íi]a|fuente|despoblado|"
        r"baron[íi]a|señor[íi]o|laguna|arroyo|r[íi]o|torre|hacienda|ribera|"
        r"fort[íi]n|castillo|fortaleza|atalaya|mirador|pico|coto|capilla|"
        r"ermita|santuario|cuart[óo]n|porci[óo]n|territorio|distrito|"
        r"partido|jurisdicci[óo]n|departamento|provincia"
        # Madoz-style lowercase abbreviations (with mandatory period).
        r"|(?:v|l|cas|c|ald|alq|parr|felig|r|hac|desp|prov|distr|cot|ant|cap|t|fr|s)\."
    r")"
)

# Strict Balearic anchor — only phrases that unambiguously refer to the
# Balearic archipelago. Critically we DO NOT include bare "Palma" or
# "Mahon", because both substrings occur in Canarian articles (``Las
# Palmas``) and would let peninsular buried lemmas through. The
# Canarian/peninsular trap is the dominant failure mode observed in
# the loose-pattern probe.
BALEARIC_ANCHOR = re.compile(
    r"(?:isla\s+de\s+(?:Mallorca|Menorca|Iv[iy]za|Ibiza|Eivissa|Formentera|Cabrera)|"
    r"prov\.?\s+(?:de\s+)?Baleares|"
    r"isla\s+y\s+(?:di[óo]c|ob)\.?\s+de\s+(?:Mallorca|Menorca|Iv[iy]za|Ibiza|Eivissa)|"
    r"part\.?\s+jud\.?\s+de\s+(?:Palma|Inca|Manacor|Mah[óo]n|Ciudadela|Ibiza|Iv[iy]za))",
    re.IGNORECASE,
)

# Hard peninsular signal — kills false positives whose body uses a
# province / archipelago name that cannot occur in a Balearic article.
# "Canarias" is the dominant trap (Madoz alphabetises islets and pagos
# of Gran Canaria / Lanzarote / Tenerife under the same letters as
# Balearic predios).
PENINSULAR_HARD = re.compile(
    r"\b(?:Canarias|Cana[nr]ias|Gran\s+Canaria|Lanzarote|Tenerife|Fuerteventura|"
    r"Aragón|Asturias|Cataluña|Galicia|Navarra|Castilla|León|Andalucía|"
    r"Extremadura|Murcia|Valencia|Granada|Sevilla|Soria|Cuenca|Lugo|Oviedo|"
    r"Pamplona|Burgos|Madrid|Toledo|Salamanca|Zaragoza|Huesca|Barbastro|"
    r"Jaca|Santiago|Tarragona|Almería|Almeria|Córdoba|Cordoba|Jaén|Jaen|"
    r"Cádiz|Cadiz|Coruña|Coruna|Pontevedra|Orense|Cáceres|Caceres|"
    r"Badajoz|Ciudad\s+Real|Albacete|Alicante|Castellón|Castellon|"
    r"Tarazona|Sigüenza|Siguenza|Logroño|Logrono|Guadalajara|"
    r"Vitoria|Bilbao|San\s+Sebastián|Gerona|Lérida|Lerida)\b"
)


def load_existing_titles() -> dict[str, set[str]]:
    """Per-volume set of normalized titles already in ``data/text/``."""
    by_vol: dict[str, set[str]] = {}
    for jp in sorted(TEXT.glob("page_*.json")):
        try:
            d = json.loads(jp.read_text())
        except Exception:
            continue
        vol = d.get("vol")
        if not vol:
            continue
        for e in d.get("entries", []):
            t = normalize(e.get("title", ""))
            if t:
                by_vol.setdefault(vol, set()).add(t)
    return by_vol


def _is_known(title: str, vol: str, by_vol: dict[str, set[str]]) -> bool:
    """True if the title fuzzy-matches something already in ``text_entries``
    for this volume.

    Threshold 88 (rather than minano's 80) because Madoz's short
    predio names (Son X, Can Y) collide on substring overlap at
    lower thresholds — ``tugores iso`` falsely scored 80 against
    ``torres so`` in early calibration. A length-disparity guard
    discards pool candidates significantly shorter than the target
    so substring scorers can't carry an unrelated short name to a
    high WRatio.
    """
    norm_t = normalize(title)
    if not norm_t:
        return True
    pool = by_vol.get(vol, set())
    nl = len(norm_t)
    for t in pool:
        if not t:
            continue
        # Length-disparity guard: ignore pool entries < 60 % of target length.
        if len(t) < 0.6 * nl:
            continue
        if fuzz.WRatio(norm_t, t) >= 88:
            return True
    return False


def scan_volume(vol: str, by_vol: dict[str, set[str]]):
    chocr = CHOCR / f"tomo{vol}.html.gz"
    if not chocr.exists():
        return []
    candidates = []
    for leaf, par_text in iter_paragraphs(chocr):
        if leaf is None or not par_text:
            continue
        norm = re.sub(r"\s+", " ", par_text).strip()
        norm = re.sub(r"(\w)-(\w)", r"\1\2", norm)
        if len(norm) < 120:
            continue
        for m in INNER_LEMMA.finditer(norm):
            # Must be buried, not at the start (start-of-para lemma is
            # the indexer's job).
            if m.start() < 30:
                continue
            title_raw = m.group(1).strip()
            first_word = re.match(r"^[A-ZÁÉÍÓÚÑÜ\d\-]+", title_raw)
            if not first_word or len(first_word.group(0)) < 3:
                continue
            # Reject titles that are just a blacklisted word.
            if title_raw.upper() in {
                "DICCIONARIO", "GEOGRAFICO", "GEOGRÁFICO", "HISTORICO",
                "HISTÓRICO", "ESPAÑA", "ULTRAMAR", "POSESIONES", "MADRID",
                "TOMO", "FIN", "ADVERTENCIA", "INDICE", "ÍNDICE",
                "ABREVIATURAS",
            }:
                continue
            tail = norm[m.end():m.end() + 250]
            if not BALEARIC_ANCHOR.search(tail):
                continue
            if PENINSULAR_HARD.search(tail):
                continue
            if _is_known(title_raw, vol, by_vol):
                continue
            ctx_start = max(0, m.start() - 60)
            ctx = norm[ctx_start:m.start() + 250]
            candidates.append({
                "vol": vol,
                "leaf": leaf,
                "title": title_raw,
                "ctx": ctx,
            })
    return candidates


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vol", help="restrict to one volume (e.g. 08)")
    ap.add_argument("--json", help="write candidates as JSONL to this path")
    args = ap.parse_args()

    if args.vol:
        vols = [args.vol.zfill(2)]
    else:
        vols = sorted(
            re.match(r"tomo(\d+)", p.name).group(1)
            for p in CHOCR.glob("tomo*.html.gz")
        )

    by_vol = load_existing_titles()
    all_candidates: list[dict] = []
    for vol in vols:
        cands = scan_volume(vol, by_vol)
        if not cands:
            print(f"=== Tom {vol}: clean ===", file=sys.stderr)
            continue
        print(f"\n=== Tom {vol}: {len(cands)} candidate(s) ===")
        for c in cands:
            print(f"\n  leaf {c['leaf']}: {c['title']!r}")
            print(f"    {c['ctx']!r}")
        all_candidates.extend(cands)

    print(
        f"\n=== Total: {len(all_candidates)} candidate(s) across {len(vols)} volume(s) ===",
        file=sys.stderr,
    )

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w") as f:
            for c in all_candidates:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        print(f"Wrote {len(all_candidates)} candidates to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
