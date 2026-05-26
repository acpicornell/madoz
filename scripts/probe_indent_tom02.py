"""Indent-detection sondeig on tom II PDF.

For each PDF page:
  1. Read all lines + their x0 via PyMuPDF.
  2. Cluster x0s to find the two column baselines (left and right body
     indent). Discards pages where bimodal structure isn't clear (front
     matter, plates, etc.).
  3. Flag every line whose x0 sits in the "opener" band:
     baseline + indent_min ≤ x0 ≤ baseline + indent_max.
  4. Within each line, validate the lemma: starts with ≥ 2 caps (OCR-
     tolerant for digits/punct inside lemma) and has a Madoz separator
     (`:` or `.` or `.—`) within the first 80 chars.
  5. Group following lines (until the next opener) as the article body.
  6. Check if the body contains a Balearic token (strict: Mallorca /
     Menorca / Ibiza / Baleares / Mahón / Eivissa / Formentera).

Output: every indent-detected Balearic opener, with its body's first 200
chars and whether it matches a text_entries(vol='02') row by fuzzy lemma.

This is exploratory — no DB writes.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import duckdb
import fitz

PROJECT = Path(__file__).resolve().parent.parent
PDF = PROJECT / "data" / "pdf" / "tomo02.pdf"
DB = PROJECT / "db" / "madoz.duckdb"

# Strict Balearic tokens (no Cabrera-only to avoid Cáceres sierra collision)
BALEARIC_STRONG = re.compile(
    r"\b(?:Mallorca|Menorca|Iviza|Ibiza|Baleares|Mah[óo]n|Eivissa|"
    r"Palma\s+de\s+Mallorca|Formentera)\b",
    re.IGNORECASE,
)

LEMMA_RE = re.compile(r"^[A-ZÁÉÍÓÚÑÜ]{2,}")
SEP_RE = re.compile(r"^[A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ0-9.\s\(\)\[\]\-,/{}]{0,80}?\s*[:.,]")

INDENT_MIN = 5
INDENT_MAX = 14
PAGE_MID_GUESS = 250


def page_lines(p):
    """Return [(x0, y0, text), ...] for body lines, sorted by y0."""
    d = p.get_text("dict")
    out = []
    for b in d["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b["lines"]:
            if not ln["spans"]:
                continue
            t = "".join(s["text"] for s in ln["spans"]).strip()
            if t:
                out.append((ln["bbox"][0], ln["bbox"][1], t))
    out.sort(key=lambda r: r[1])
    return out


def cluster_baselines(lines):
    """Find left + right column baselines using x0 distribution.

    Splits at a dynamic gutter (the page width's midpoint won't do — some
    leaves drift). The gutter is the lowest-density x position between
    the two largest x0 clusters.
    """
    xs = [int(x) for x, _, _ in lines]
    if not xs:
        return None, None
    # Histogram in 5pt bins
    bins = Counter(x // 5 * 5 for x in xs)
    # Two main modes by count
    sorted_bins = sorted(bins.items(), key=lambda kv: -kv[1])
    if len(sorted_bins) < 2:
        return None, None
    # Take the top two non-adjacent bins as candidates
    top_x0 = sorted_bins[0][0]
    second_x0 = None
    for x, _ in sorted_bins[1:]:
        if abs(x - top_x0) > 80:
            second_x0 = x
            break
    if second_x0 is None:
        return None, None
    lb = min(top_x0, second_x0)
    rb = max(top_x0, second_x0)
    return lb, rb


def detect_openers(lines, lb, rb):
    """List of (x0, y0, lemma, full_line) where line is a candidate opener."""
    if lb is None or rb is None:
        return []
    gutter = (lb + rb) / 2
    out = []
    for x, y, t in lines:
        baseline = lb if x < gutter else rb
        off = x - baseline
        if not (INDENT_MIN <= off <= INDENT_MAX):
            continue
        if not LEMMA_RE.match(t):
            continue
        if not SEP_RE.match(t):
            continue
        m = re.match(
            r"^([A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ0-9.\s\(\)\[\]\-,/{}]{0,80}?)\s*[:.,]",
            t,
        )
        lemma = m.group(1).strip() if m else None
        if not lemma:
            continue
        out.append((x, y, lemma, t))
    return out


def collect_article_bodies(doc):
    """Walk every page, detect openers, and for each opener collect the
    lines until the next opener on the same column.

    Returns list of {page, lemma, opener_line, body (concatenated), x_side}.
    """
    articles = []
    for pp in range(doc.page_count):
        lines = page_lines(doc[pp])
        if not lines:
            continue
        lb, rb = cluster_baselines(lines)
        if lb is None:
            continue
        openers = detect_openers(lines, lb, rb)
        if not openers:
            continue
        # Partition into left and right column lines
        gutter = (lb + rb) / 2
        left_lines = [(y, x, t) for x, y, t in lines if x < gutter]
        right_lines = [(y, x, t) for x, y, t in lines if x >= gutter]
        left_lines.sort()
        right_lines.sort()
        for x_op, y_op, lemma, opener_line in openers:
            col = "L" if x_op < gutter else "R"
            col_lines = left_lines if col == "L" else right_lines
            # Find this opener's index in col_lines (closest by y)
            i_start = None
            for i, (y, _, t) in enumerate(col_lines):
                if abs(y - y_op) < 0.5 and lemma in t:
                    i_start = i
                    break
            if i_start is None:
                continue
            # Next opener in same column
            next_y = float("inf")
            for x2_op, y2_op, _, _ in openers:
                col2 = "L" if x2_op < gutter else "R"
                if col2 != col:
                    continue
                if y2_op > y_op + 1 and y2_op < next_y:
                    next_y = y2_op
            body_lines = []
            for y, _, t in col_lines[i_start + 1:]:
                if y >= next_y:
                    break
                body_lines.append(t)
            body = " ".join(body_lines)
            articles.append({
                "page": pp, "lemma": lemma, "opener_line": opener_line,
                "body": body, "col": col,
                "full_text": opener_line + " " + body,
            })
    return articles


def main():
    doc = fitz.open(str(PDF))
    print(f"Reading {PDF.name}: {doc.page_count} pages")
    articles = collect_article_bodies(doc)
    print(f"\nIndent-detected article openers (all): {len(articles)}")

    # Filter to Balearic body
    balearic = [a for a in articles if BALEARIC_STRONG.search(a["full_text"])]
    print(f"Indent-detected articles with Balearic content: {len(balearic)}")

    # Compare to existing text_entries(vol='02')
    con = duckdb.connect(str(DB), read_only=True)
    text02 = [r[0] for r in con.execute(
        "SELECT title FROM text_entries WHERE vol='02'"
    ).fetchall()]
    chocr02 = [r[0] for r in con.execute(
        "SELECT title FROM chocr_entries WHERE vol='02'"
    ).fetchall()]
    print(f"\ntext_entries(vol='02'): {len(text02)}")
    print(f"chocr_entries(vol='02'): {len(chocr02)}")

    # Aggressively-compressed lemma comparison
    import unicodedata
    def compress(s):
        if not s:
            return ""
        s = s.upper()
        s = "".join(c for c in unicodedata.normalize("NFD", s)
                    if unicodedata.category(c) != "Mn")
        s = re.sub(r"[^A-Z0-9]", "", s)
        return s

    from rapidfuzz import fuzz
    text_compressed = [(t, compress(t)) for t in text02]
    chocr_compressed = [(t, compress(t)) for t in chocr02]

    matched_text = 0
    matched_chocr_only = 0
    novel = []
    for a in balearic:
        lc = compress(a["lemma"])
        if not lc:
            continue
        hit_text = False
        for orig, c in text_compressed:
            if not c:
                continue
            if lc == c or lc in c or c in lc:
                hit_text = True
                break
            if fuzz.ratio(lc, c) >= 70:
                hit_text = True
                break
        if hit_text:
            matched_text += 1
            continue
        hit_chocr = False
        for orig, c in chocr_compressed:
            if not c:
                continue
            if lc == c or lc in c or c in lc:
                hit_chocr = True
                break
            if fuzz.ratio(lc, c) >= 70:
                hit_chocr = True
                break
        if hit_chocr:
            matched_chocr_only += 1
            # Article exists in chocr_entries but not promoted to text_entries
            novel.append((a, "in chocr, missing from text"))
        else:
            novel.append((a, "novel — not in chocr or text"))

    print(f"\nIndent-detected Balearic articles already in text_entries: {matched_text}")
    print(f"Indent-detected Balearic articles in chocr but not text: {matched_chocr_only}")
    print(f"Truly novel (not in any DB table): {len(novel) - matched_chocr_only}")
    print()
    print("=== Novel and not-promoted-from-chocr cases ===")
    for a, tag in novel:
        body_snippet = a["body"][:150]
        print(f"  [{tag}] pdf {a['page']:3d} col={a['col']}  lemma=\"{a['lemma'][:30]:30s}\"")
        print(f"    opener: {a['opener_line'][:120]}")
        print(f"    body  : {body_snippet}")
        print()


if __name__ == "__main__":
    main()
