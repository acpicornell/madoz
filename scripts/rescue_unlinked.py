"""Rescue chocr_entries that have Balearic content but no text_entry
covering them on the same leaf. The OCR-paragraph index catches a
Balearic article that the LLM-extraction phase dropped (because the
chocr title was OCR-mangled beyond recognition, because the
extraction window cropped the wrong paragraph, etc.).

This is the only phase that survived the diccionariomadoz.com mirror
removal. The earlier four phases relied on the curated mirror as a
ground-truth set; they no longer have a target.

Inserts a text_entries row with confidence='unverified' and
model='chocr-snippet' so the cleanup_unverified.py pass can later
normalise OCR artefacts and verify titles against Tesseract or PDF.

Usage:
  python scripts/rescue_unlinked.py            # dry-run
  python scripts/rescue_unlinked.py --apply    # commit
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
CHOCR_DIR = PROJECT / "data" / "text" / "_chocr"


BALEARIC_TOKENS = re.compile(
    r"\b(?:Mallorca|Menorca|Iviza|Ibiza|Baleares|Cabrera|Formentera|"
    r"Palma\s+de\s+Mallorca|Mah[óo]n|Ciudadela|Eivissa)\b",
    re.IGNORECASE,
)

_BALEARIC_COMPRESSED = re.compile(
    r"(?:mallorca|menorca|iviza|ibiza|baleares|cabrera|formentera|"
    r"palmademallorca|mahon|ciudadela|eivissa)"
)
_BALEARIC_STRONG_COMPRESSED = re.compile(
    r"(?:mallorca|menorca|iviza|ibiza|baleares|mahon|eivissa|"
    r"palmademallorca|formentera)"
)


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def is_balearic_text(text: str) -> bool:
    """True iff the body unambiguously describes a Balearic place.
    Rejects peninsular articles that mention 'Cabrera' in passing
    (sierra de la Cabrera in Cáceres) or list Balearic suffragans
    (VALENCIA archbishop mentioning Mallorca and Menorca)."""
    if not text:
        return False
    compressed = _strip_accents(text).lower()
    compressed = re.sub(r"[^a-z]+", "", compressed)
    if not _BALEARIC_COMPRESSED.search(compressed):
        return False
    if not _BALEARIC_STRONG_COMPRESSED.search(compressed):
        return False
    head = text[:120].lower()
    if re.search(
        r"\b(?:prov(?:incia)?\.?|part(?:ido)?\.?\s*jud\.?|di[óo]c\.?|"
        r"audiencia|tercio|distrito)\s+(?:territoriales?|de)\s+"
        r"(?:c[áa]ceres|badajoz|c[óo]rdoba|granada|sevilla|c[áa]diz|"
        r"valencia|alicante|murcia|cartagena|toledo|madrid|barcelona|"
        r"gerona|tarragona|l[ée]rida|huesca|zaragoza|teruel|"
        r"navarra|guip[úu]zcoa|vizcaya|[áa]lava|la\s+coru[ñn]a|"
        r"lugo|orense|pontevedra|asturias?|oviedo|le[óo]n|"
        r"salamanca|[áa]vila|segovia|valladolid|burgos|santander|"
        r"palencia|soria|guadalajara|cuenca|ciudad\s+real|albacete|"
        r"ja[ée]n|almer[íi]a|m[áa]laga|huelva|c[áa]diz|canarias|tenerife)\b",
        head,
    ):
        return False
    return True


PT_OPENERS = [
    (re.compile(r"^\s*v\.\s+con", re.I),       "villa"),
    (re.compile(r"^\s*v\.\s+cab", re.I),       "villa"),
    (re.compile(r"^\s*villa\b", re.I),         "villa"),
    (re.compile(r"^\s*c\.\s+con", re.I),       "ciudad"),
    (re.compile(r"^\s*ciudad\b", re.I),        "ciudad"),
    (re.compile(r"^\s*l\.\s+(?:con|de|en)", re.I), "lugar"),
    (re.compile(r"^\s*lugar\b", re.I),         "lugar"),
    (re.compile(r"^\s*ald\.\b", re.I),         "aldea"),
    (re.compile(r"^\s*aldea\b", re.I),         "aldea"),
    (re.compile(r"^\s*cas(?:erío|\.)\b", re.I),"caserío"),
    (re.compile(r"^\s*predio\s+con", re.I),    "predio"),
    (re.compile(r"^\s*predio\b", re.I),        "predio"),
    (re.compile(r"^\s*alqu[eé]r[ií]a\b", re.I),"alquería"),
    (re.compile(r"^\s*finca\b", re.I),         "finca"),
    (re.compile(r"^\s*cabo\b", re.I),          "cabo"),
    (re.compile(r"^\s*cala\b", re.I),          "cala"),
    (re.compile(r"^\s*bah[ií]a\b", re.I),      "bahía"),
    (re.compile(r"^\s*isla\b", re.I),          "isla"),
    (re.compile(r"^\s*isleta\b", re.I),        "isleta"),
    (re.compile(r"^\s*islote\b", re.I),        "islote"),
    (re.compile(r"^\s*r[ií]o\b|^\s*r\.\b", re.I), "río"),
    (re.compile(r"^\s*arroyo\b", re.I),        "arroyo"),
    (re.compile(r"^\s*sierra\b", re.I),        "sierra"),
    (re.compile(r"^\s*monte\b", re.I),         "monte"),
    (re.compile(r"^\s*partido\s+jud", re.I),   "partido judicial"),
    (re.compile(r"^\s*part\.\s*jud", re.I),    "partido judicial"),
    (re.compile(r"^\s*di[óo]c", re.I),         "diócesis"),
    (re.compile(r"^\s*audiencia\b", re.I),     "audiencia"),
    (re.compile(r"^\s*provincia\b", re.I),     "provincia"),
    (re.compile(r"^\s*felig", re.I),           "feligresía"),
    (re.compile(r"^\s*parroquia\b", re.I),     "parroquia"),
    (re.compile(r"^\s*balsa\b", re.I),         "balsa"),
    (re.compile(r"^\s*torre\b", re.I),         "torre"),
    (re.compile(r"^\s*puerto\b", re.I),        "puerto"),
]


def infer_place_type(context_or_para: str | None) -> str | None:
    if not context_or_para:
        return None
    s = re.sub(r"^[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ.\s\(\)\-,]{0,80}?[:.,]\s*", "", context_or_para)
    for pat, pt in PT_OPENERS:
        if pat.match(s):
            return pt
    return None


_DIGIT_FIX = str.maketrans({"1": "I", "0": "O", "5": "S", "8": "B", "4": "A"})


def compressed_lemma(title: str) -> str:
    """Aggressive normalisation for cross-source title dedup.
    Strips all whitespace, accents, punctuation, applies OCR-digit
    substitutions."""
    if not title:
        return ""
    s = title.upper()
    s = _strip_accents(s)
    s = re.sub(r"[^A-Z0-9]", "", s)
    s = s.translate(_DIGIT_FIX)
    return s


def title_covered_on_leaf(chocr_title: str, leaf_text_titles: list[str]) -> bool:
    ck = compressed_lemma(chocr_title)
    if not ck:
        return False
    from rapidfuzz import fuzz
    for t in leaf_text_titles:
        tk = compressed_lemma(t)
        if not tk:
            continue
        if ck == tk or ck in tk or tk in ck:
            return True
        if fuzz.ratio(ck, tk) >= 80:
            return True
    return False


def _load_chocr_window(vol: str, leaf: int) -> str | None:
    p = CHOCR_DIR / f"page_{vol}_{leaf}.txt"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def _extract_paragraph(chocr_text: str, lemma_upper: str) -> str | None:
    """Find the Balearic paragraph in the chocr window that opens with
    the given lemma. Picks the longest Balearic-token-bearing match
    among ambiguous candidates."""
    cleaned = re.sub(r"^=== [^=\n]+ ===\s*$", "", chocr_text, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*#.*$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*\d{1,4}\s+[A-Z]{2,4}\s*$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    paras = re.split(r"\n\s*\n", cleaned)
    pat = re.compile(
        rf"^\s*{re.escape(lemma_upper)}\b(?:\s*\([^)]*\))?\s*[:.,]",
        re.MULTILINE,
    )
    matches: list[str] = []
    for para in paras:
        m = pat.search(para)
        if m:
            matches.append(para[m.start():].strip())
    if not matches:
        return None
    bal = [p for p in matches if BALEARIC_TOKENS.search(p)]
    pool = bal if bal else matches
    return max(pool, key=len)


PENINSULAR_LEMMA_BLOCK = {
    "VALENCIA", "BARCELONA", "MADRID", "SEVILLA", "ZARAGOZA",
    "TOLEDO", "CORDOBA", "GRANADA", "MURCIA", "CARTAGENA",
    "MALAGA", "BILBAO", "OVIEDO", "BURGOS", "LEON",
    "SALAMANCA", "AVILA", "SEGOVIA", "VALLADOLID", "PALENCIA",
    "SANTANDER", "NAVARRA", "PAMPLONA", "VITORIA", "SAN SEBASTIAN",
    "LUGO", "ORENSE", "PONTEVEDRA", "LA CORUNA", "GERONA",
    "TARRAGONA", "LERIDA", "HUESCA", "TERUEL", "CASTELLON",
    "ALICANTE", "ALMERIA", "JAEN", "HUELVA", "CACERES",
    "BADAJOZ", "CIUDAD REAL", "CUENCA", "GUADALAJARA", "SORIA",
    "CADIZ", "CANARIAS", "TENERIFE", "ALBACETE",
}


def _is_peninsular_lemma(title: str) -> bool:
    if not title:
        return False
    bare = re.sub(r"\([^)]*\)", "", title).strip()
    bare = re.sub(r"\s+", " ", bare).upper()
    bare = _strip_accents(bare)
    return bare in PENINSULAR_LEMMA_BLOCK


def _looks_like_real_lemma(title: str) -> bool:
    if not title:
        return False
    if len(title) > 50:
        return False
    bare = re.sub(r"\([^)]*\)", "", title).strip()
    bare = re.sub(r"\s+", " ", bare)
    if not bare:
        return False
    if re.search(r"\b[a-z]+\b", bare):
        return False
    return True


def promote_chocr_orphans(con: duckdb.DuckDBPyConnection, *, apply: bool) -> list[dict]:
    """For each chocr_entry that has Balearic content AND no text_entry
    covering it on the same (vol, leaf±2), insert a new text_entries row.

    Madoz-Castilian title cross-leaf dedup is loose: if any text_entry
    in the same volume has a near-identical compressed lemma, the
    chocr entry is considered already represented."""
    chocr_rows = con.execute("""
        SELECT id, vol, leaf, page_printed, title, context
        FROM chocr_entries
        WHERE source = 'regex'
          AND NOT EXISTS (
              SELECT 1 FROM text_entries t
              WHERE t.chocr_entry_id = chocr_entries.id
          )
    """).fetchall()

    text_by_leaf: dict[tuple, list[tuple]] = {}
    text_by_vol: dict[str, list[str]] = {}
    for r in con.execute(
        "SELECT id, vol, leaf, title FROM text_entries"
    ).fetchall():
        text_by_leaf.setdefault((r[1], r[2]), []).append((r[0], r[3]))
        text_by_vol.setdefault(r[1], []).append(r[3])

    next_id = (con.execute("SELECT max(id) FROM text_entries").fetchone()[0] or 0) + 1

    proposed = []
    for cid, vol, leaf, pp, ctitle, ctx in chocr_rows:
        if not _looks_like_real_lemma(ctitle):
            continue
        if _is_peninsular_lemma(ctitle):
            continue
        if not is_balearic_text(ctx):
            continue
        leaf_titles = [t for _, t in text_by_leaf.get((vol, leaf), [])]
        if title_covered_on_leaf(ctitle, leaf_titles):
            continue
        ck = compressed_lemma(ctitle)
        vol_titles = text_by_vol.get(vol, [])
        if ck and any(compressed_lemma(t) == ck for t in vol_titles):
            continue

        window = _load_chocr_window(vol, leaf)
        if window is None:
            para = ctx
        else:
            lemma_for_lookup = ctitle.split("(")[0].strip()
            para = _extract_paragraph(window, lemma_for_lookup)
            if para is None:
                para = ctx
        if not para or len(para) < 60:
            continue
        if not is_balearic_text(para):
            continue

        chocr_pt = infer_place_type(ctx) or infer_place_type(para)
        proposed.append({
            "new_tid": next_id, "vol": vol, "leaf": leaf,
            "page_printed": pp, "title": ctitle, "place_type": chocr_pt,
            "description": para[:8000],
            "chocr_entry_id": cid,
            "confidence": "unverified",
            "note": "Promoted by rescue_unlinked.py (chocr orphan, no same-leaf text_entry)",
            "source_file": (
                f"data/text/_chocr/page_{vol}_{leaf}.txt"
                if window else "chocr_entries.context"
            ),
        })
        text_by_leaf.setdefault((vol, leaf), []).append((next_id, ctitle))
        text_by_vol.setdefault(vol, []).append(ctitle)
        next_id += 1

    if apply and proposed:
        for p in proposed:
            con.execute(
                """INSERT INTO text_entries
                   (id, vol, leaf, page_printed, title, place_type,
                    description, confidence, model, source_file,
                    note, chocr_entry_id, extracted_at, description_raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), ?)""",
                [
                    p["new_tid"], p["vol"], p["leaf"], p["page_printed"],
                    p["title"], p["place_type"], p["description"],
                    p["confidence"], "chocr-snippet",
                    p["source_file"], p["note"], p["chocr_entry_id"],
                    p["description"],
                ],
            )
    return proposed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Commit changes. Default is dry-run.")
    args = ap.parse_args()
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    con = duckdb.connect(str(DB), read_only=not args.apply)

    before_n = con.execute("SELECT count(*) FROM text_entries").fetchone()[0]
    proposed = promote_chocr_orphans(con, apply=args.apply)
    after_n = con.execute("SELECT count(*) FROM text_entries").fetchone()[0]

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"\n=== Promote chocr orphans ({len(proposed)} proposals) [{mode}] ===")
    for p in proposed[:30]:
        print(f"  new text {p['new_tid']}: \"{p['title'][:35]:35s}\""
              f" vol={p['vol']} leaf={p['leaf']} pt={p['place_type'] or '—':12s}"
              f" desc-len={len(p['description'])}")
    if len(proposed) > 30:
        print(f"  ... and {len(proposed) - 30} more")
    print(f"\ntext_entries: {before_n} → {after_n}  ({'+' if after_n > before_n else ''}{after_n - before_n})")


if __name__ == "__main__":
    main()
