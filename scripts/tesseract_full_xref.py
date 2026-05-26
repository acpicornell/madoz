"""Full-corpus cross-reference: every Tesseract OCR text file vs the
DB tables text_entries + chocr_entries.

For each of the ~11,894 Tesseract pages:
  1. Parse candidate article openers (all-caps lemma + Madoz separator).
  2. Group lines into body chunks (between openers).
  3. Filter to bodies with strong Balearic content (Mallorca / Menorca /
     Ibiza / Baleares / Mahón / Eivissa / Formentera in the first 200
     chars, NOT just Cabrera which collides with Sierra de la Cabrera).
  4. For each Balearic Tesseract opener, fuzzy-match against
     text_entries(vol=v) and chocr_entries(vol=v) titles.

Outputs three buckets:
  NOVEL          — Tesseract opener with no fuzzy match in either DB
                   table on the same volume. Real candidates to inspect.
  CLEANER_TITLE  — Tesseract lemma differs from a matched DB title that
                   shows ABBYY OCR-mangling. Tesseract reading wins.
  KNOWN          — Tesseract finds the same article we already have.

Speed: ~5–10 minutes (text files, no OCR re-run, no API).
"""
from __future__ import annotations

import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
TESS_DIR = PROJECT / "data" / "tesseract" / "text"
OUT_PATH = PROJECT / "data" / "tesseract" / "xref_report.txt"

# --- Filters ---------------------------------------------------------------

# Strong Balearic tokens. 'Cabrera' alone is ambiguous (Sierra de la
# Cabrera in Cáceres) so we require co-occurrence with one of these.
BALEARIC_STRONG = re.compile(
    r"\b(?:Mallorca|Menorca|Iviza|Ibiza|Baleares|Mah[óo]n|Eivissa|"
    r"Palma\s+de\s+Mallorca|Formentera)\b",
    re.IGNORECASE,
)

# Peninsular toponyms that mention Balearic places as suffragans /
# cross-references. Used to reject false positives like 'VALENCIA
# (Arzobispado de)' that pass the Balearic strong filter only because
# the body lists Mallorca / Menorca as dioceses.
PENINSULAR_HEAD = re.compile(
    r"\b(?:prov(?:incia)?\.?|part(?:ido)?\.?\s*jud\.?|di[óo]c\.?)\s+"
    r"(?:de\s+)?"
    r"(?:c[áa]ceres|badajoz|c[óo]rdoba|granada|sevilla|c[áa]diz|"
    r"valencia|alicante|murcia|cartagena|toledo|madrid|barcelona|"
    r"gerona|tarragona|l[ée]rida|huesca|zaragoza|teruel|"
    r"navarra|guip[úu]zcoa|vizcaya|[áa]lava|la\s+coru[ñn]a|"
    r"lugo|orense|pontevedra|asturias?|oviedo|le[óo]n|"
    r"salamanca|[áa]vila|segovia|valladolid|burgos|santander|"
    r"palencia|soria|guadalajara|cuenca|ciudad\s+real|albacete|"
    r"ja[ée]n|almer[íi]a|m[áa]laga|huelva|canarias|tenerife|"
    r"castell[óo]n)\b",
    re.IGNORECASE,
)

# Opener: lemma starts a line (after optional whitespace), all caps with
# at least 2 letters, optional parens / hyphen / spaces, followed by a
# Madoz separator. The lemma is bounded at ≤60 chars (Madoz titles are
# usually 5-30 chars; allow generous limit).
OPENER_RE = re.compile(
    r"^\s*([A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ.\s\(\)\-,/]{2,60}?)\s*[:.,;](?=\s*[a-záéíóúñ0-9])",
    re.MULTILINE,
)

# Junk lemma filters: skip lines that are 3-letter running headers, or
# obviously body fragments masquerading as openers
JUNK_LEMMAS = re.compile(
    r"^(?:"
    r"[A-Z]{2,3}|"                                  # 2-3 letter (ARI, ESP, MAL)
    r"DE|EN|EL|LA|LAS|LOS|LE|"                      # function words
    r"TOMO|FIN|ADVERT|PROLOG|INDICE|"               # front-matter signals
    r"NOTA|HIST|HISTORIA|ESPA[ÑN]A|ULTRAMAR|"       # editorial markers
    r"GEOGR[ÁA]FICO|HIST[ÓO]RICO|DICCIONARIO|"
    r"PROD\.?|POBL\.?|CONTR\.?|RIQU?EZA|SIT\.?|"    # body section markers
    r"IND\.?|TERRENO|CLIMA|CONFINA"
    r")$"
)

# Madoz-style opener heuristic — body should start with one of the
# standard place-type abbreviations or near-standard wording. Used as a
# secondary filter to reduce Tesseract false positives.
MADOZ_BODY_OPENER = re.compile(
    r"^\s*(?:"
    r"v\.|villa\b|"
    r"c\.|ciudad\b|"
    r"l\.|1\.|lugar\b|"          # Tesseract often reads l. as 1.
    r"ald\.|aldea\b|"
    r"cas\.?\b|caser[ií]o\b|"
    r"predio\b|prédio\b|alqu[eé]r[ií]a\b|alq\.|finca\b|cortijo\b|hacienda\b|"
    r"cabo\b|cala\b|bah[ií]a\b|b[áa]hia\b|punta\b|"
    r"isla\b|isleta\b|islote\b|"
    r"r[ií]o\b|r\.\b|arroyo\b|sierra\b|monte\b|"
    r"partido\s+jud|part\.\s*jud|"
    r"di[óo]c|audiencia|provincia|tercio|"
    r"felig\.|feligres[ií]a|parroquia|"
    r"balsa|torre|puerto|valle|granja|"
    r"casa\s+de\s+campo|atalaya|"
    r"desp\.|despoblad"
    r")",
    re.IGNORECASE,
)


# --- Helpers ---------------------------------------------------------------


def compress(s: str) -> str:
    if not s:
        return ""
    s = s.upper()
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn")
    return re.sub(r"[^A-Z0-9]", "", s)


def is_strong_balearic(text: str) -> bool:
    """True iff text unambiguously describes a Balearic place."""
    if not text:
        return False
    if not BALEARIC_STRONG.search(text):
        return False
    head = text[:140].lower()
    if PENINSULAR_HEAD.search(head):
        return False
    return True


def looks_like_real_opener(lemma: str, body_head: str) -> bool:
    """Reject junk lemmas + bodies that don't look like Madoz prose."""
    bare = re.sub(r"\([^)]*\)", "", lemma).strip()
    bare = re.sub(r"\s+", " ", bare).upper()
    if not bare or len(bare) > 50:
        return False
    if JUNK_LEMMAS.match(bare):
        return False
    # Real Madoz lemma: at least 4 letters in bare, dominated by caps
    if not re.match(r"^[A-ZÁÉÍÓÚÑÜ]{4,}", bare):
        return False
    # Body must look like Madoz prose (place-type opener)
    if not MADOZ_BODY_OPENER.match(body_head):
        return False
    return True


def extract_openers(text: str) -> list[tuple[str, str]]:
    matches = list(OPENER_RE.finditer(text))
    out = []
    for i, m in enumerate(matches):
        lemma = m.group(1).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        out.append((lemma, body))
    return out


# --- Main ------------------------------------------------------------------


def main():
    from rapidfuzz import fuzz

    con = duckdb.connect(str(DB), read_only=True)
    text_by_vol = defaultdict(list)
    for r in con.execute("SELECT vol, title FROM text_entries").fetchall():
        text_by_vol[r[0]].append((r[1], compress(r[1])))
    chocr_by_vol = defaultdict(list)
    for r in con.execute(
        "SELECT vol, title FROM chocr_entries WHERE source='regex'"
    ).fetchall():
        chocr_by_vol[r[0]].append((r[1], compress(r[1])))

    # Also build a fuzzy "any title on the same volume" check for the
    # CLEANER_TITLE detection (we want to flag cases where Tesseract gives
    # a clean lemma that maps to a mangled ABBYY title).
    MANGLE_RE = re.compile(
        r"(\d|[¡¿|\\/`\"]|[A-Z]ll?[A-Z]|^[A-Z]{1,3}\s+[A-Z]|[A-Z]{2,}I{2,})"
    )

    def find_match(ck: str, candidates: list[tuple[str, str]]) -> tuple[str, str] | None:
        """Return (orig_title, kind) where kind ∈ {'exact','substr','fuzz75'}."""
        if not ck:
            return None
        best_fuzz = (0, None)
        for orig, c in candidates:
            if not c:
                continue
            if ck == c:
                return orig, "exact"
            if ck in c or c in ck:
                return orig, "substr"
            score = fuzz.ratio(ck, c)
            if score > best_fuzz[0]:
                best_fuzz = (score, orig)
        if best_fuzz[0] >= 75:
            return best_fuzz[1], f"fuzz{best_fuzz[0]:.0f}"
        return None

    novel = []
    cleaner_titles = []
    known = 0
    rejected_junk = 0
    rejected_non_balearic = 0
    t_start = time.time()

    files = sorted(TESS_DIR.glob("tomo*_p*.txt"))
    print(f"Scanning {len(files)} Tesseract text files...\n")

    for i, fp in enumerate(files):
        # tomoNN_pNNNN.txt — extract vol and pdf page
        m = re.match(r"tomo(\d{2})_p(\d{4})\.txt$", fp.name)
        if not m:
            continue
        vol = m.group(1)
        pdf_p = int(m.group(2))
        text = fp.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            continue
        for lemma, body in extract_openers(text):
            # Strong Balearic content check
            full_head = lemma + " " + body[:300]
            if not is_strong_balearic(full_head):
                rejected_non_balearic += 1
                continue
            if not looks_like_real_opener(lemma, body[:120]):
                rejected_junk += 1
                continue

            ck = compress(lemma)
            if len(ck) < 4:
                continue

            text_match = find_match(ck, text_by_vol[vol])
            chocr_match = find_match(ck, chocr_by_vol[vol])

            if text_match:
                orig, kind = text_match
                # Cleaner title: text_entry has a mangled title and
                # Tesseract gives a clean reading
                if (
                    compress(orig) != ck
                    and MANGLE_RE.search(orig)
                    and not MANGLE_RE.search(lemma)
                ):
                    cleaner_titles.append({
                        "vol": vol, "pdf_p": pdf_p,
                        "ours": orig, "tess": lemma, "kind": kind,
                        "in": "text_entries",
                        "body_head": body[:120],
                    })
                else:
                    known += 1
                continue
            if chocr_match:
                orig, kind = chocr_match
                if (
                    compress(orig) != ck
                    and MANGLE_RE.search(orig)
                    and not MANGLE_RE.search(lemma)
                ):
                    cleaner_titles.append({
                        "vol": vol, "pdf_p": pdf_p,
                        "ours": orig, "tess": lemma, "kind": kind,
                        "in": "chocr_entries",
                        "body_head": body[:120],
                    })
                else:
                    known += 1
                continue
            # No match in either table on this volume → NOVEL
            novel.append({
                "vol": vol, "pdf_p": pdf_p, "lemma": lemma,
                "body_head": body[:200],
            })

    elapsed = time.time() - t_start

    # Dedup novel by (vol, compressed lemma) — same article on adjacent
    # leaves shows up multiple times via Tesseract; collapse to one.
    seen_keys = set()
    novel_dedup = []
    for n in novel:
        key = (n["vol"], compress(n["lemma"]))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        novel_dedup.append(n)

    # Same dedup for cleaner_titles by (vol, ours_compressed)
    seen_keys = set()
    cleaner_dedup = []
    for c in cleaner_titles:
        key = (c["vol"], compress(c["ours"]))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        cleaner_dedup.append(c)

    # Write the full report
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w") as f:
        f.write(f"# Tesseract full-corpus cross-reference\n")
        f.write(f"# Generated in {elapsed:.1f}s over {len(files)} pages\n")
        f.write(f"# Known matches (no action): {known}\n")
        f.write(f"# Rejected as junk lemma: {rejected_junk}\n")
        f.write(f"# Rejected non-Balearic: {rejected_non_balearic}\n")
        f.write(f"# CLEANER_TITLE candidates: {len(cleaner_dedup)}\n")
        f.write(f"# NOVEL candidates: {len(novel_dedup)}\n\n")
        f.write("## CLEANER_TITLE (Tesseract gives a clean lemma, ABBYY has mangled)\n\n")
        for c in cleaner_dedup:
            f.write(f"v{c['vol']} pdf{c['pdf_p']:04d}  ours={c['ours']!r}"
                    f" → tess={c['tess']!r} (in {c['in']}, match {c['kind']})\n")
            f.write(f"  body: {c['body_head']}\n\n")
        f.write("\n## NOVEL (Tesseract finds article we don't have anywhere)\n\n")
        for n in novel_dedup:
            f.write(f"v{n['vol']} pdf{n['pdf_p']:04d}  \"{n['lemma']}\"\n")
            f.write(f"  body: {n['body_head']}\n\n")

    print(f"Elapsed: {elapsed:.1f}s\n")
    print(f"Known matches: {known}")
    print(f"CLEANER_TITLE candidates (after dedup): {len(cleaner_dedup)}")
    print(f"NOVEL candidates (after dedup): {len(novel_dedup)}")
    print(f"\nFull report: {OUT_PATH}\n")

    if cleaner_dedup:
        print("=== CLEANER_TITLE preview (first 15) ===")
        for c in cleaner_dedup[:15]:
            print(f"  v{c['vol']}: \"{c['ours']}\" → \"{c['tess']}\" "
                  f"({c['in']}, {c['kind']})")

    if novel_dedup:
        print("\n=== NOVEL preview (first 25) ===")
        for n in novel_dedup[:25]:
            print(f"  v{n['vol']} pdf{n['pdf_p']:04d}: \"{n['lemma']}\"")
            print(f"    {n['body_head'][:120]}")


if __name__ == "__main__":
    main()
