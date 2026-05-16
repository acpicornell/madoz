"""Find leaves where the chocr text has MORE Balearic articles than
text_entries captured. These are missing-homonym candidates: Madoz
prints "REFALET" four times on a single leaf (one for each village)
and our extraction only grabbed one.

Heuristic:
  1. For each leaf with extracted entries, parse the chocr text into
     article-paragraphs.
  2. Filter to those mentioning a Balearic island/province.
  3. Group by normalized leading title; count occurrences.
  4. Compare to text_entries.count by (vol, leaf, normalized_title).
  5. Flag (vol, leaf, title) where chocr-count > our-count.

The detection has false positives (OCR noise, peninsular homonyms
matching loose patterns) so the output is a candidate list, not a
verdict. Use it to spot-check pages and run targeted recoveries.

  python scripts/audit_homonyms.py
  python scripts/audit_homonyms.py --html       # write data/reports/missing_homonyms.html
"""
from __future__ import annotations
import argparse
import re
import sys
import unicodedata
from collections import defaultdict
from html import escape
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
CHOCR_DIR = PROJECT / "data" / "text" / "_chocr"
OUT_HTML = PROJECT / "data" / "reports" / "missing_homonyms.html"

# Tokens that mark a Balearic article body.
BAL_RE = re.compile(
    r"\b(mallorca|menorca|ibiza|iviza|formentera|cabrera|baleares|balear)\b",
    re.IGNORECASE,
)
# An article starts with an uppercase title (≥2 chars) followed by ":" or " -"
# (optionally with a parenthetical specifier).
ARTICLE_HEAD_RE = re.compile(
    r"^([A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ\s\(\)\.\-',!]{1,60}?)\s*[:\-]\s",
)


def normalize_title(t: str) -> str:
    """Normalize for comparison: strip punctuation, accents, parens content."""
    if not t:
        return ""
    s = re.sub(r"\([^)]*\)", " ", t)
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_chocr_articles(text: str) -> list[tuple[str, str]]:
    """Split a chocr text file into (title, body) article tuples."""
    body = text
    if "=== TARGET LEAF ===" in body:
        body = body.split("=== TARGET LEAF ===", 1)[1]
    if "=== CONTINUATION" in body:
        body = body.split("=== CONTINUATION", 1)[0]
    paragraphs = re.split(r"\n\s*\n+", body)
    articles = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        m = ARTICLE_HEAD_RE.match(para)
        if m:
            articles.append((m.group(1).strip(), para))
    return articles


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--html", action="store_true", help="write HTML report too")
    args = ap.parse_args()

    con = duckdb.connect(str(DB), read_only=True)
    rows = con.execute(
        "SELECT vol, leaf, title FROM text_entries"
    ).fetchall()
    # Index our entries: count by (vol, leaf, norm_title)
    our_counts: dict[tuple[str, int, str], int] = defaultdict(int)
    leaf_titles: dict[tuple[str, int], set[str]] = defaultdict(set)
    for vol, leaf, title in rows:
        nt = normalize_title(title)
        our_counts[(vol, leaf, nt)] += 1
        leaf_titles[(vol, leaf)].add(title)

    leaves = sorted(leaf_titles.keys())
    candidates = []
    for vol, leaf in leaves:
        path = CHOCR_DIR / f"page_{vol}_{leaf}.txt"
        if not path.exists():
            continue
        articles = parse_chocr_articles(path.read_text(encoding="utf-8", errors="ignore"))
        # Balearic-only article counts on this leaf
        choc_counts: dict[str, int] = defaultdict(int)
        chocr_titles: dict[str, list[str]] = defaultdict(list)
        for title, body in articles:
            if BAL_RE.search(body):
                nt = normalize_title(title)
                if not nt:
                    continue
                choc_counts[nt] += 1
                chocr_titles[nt].append(title)
        # Compare to our text_entries on this leaf
        for nt, n_choc in choc_counts.items():
            n_ours = our_counts.get((vol, leaf, nt), 0)
            if n_choc > n_ours and n_choc >= 2:
                candidates.append({
                    "vol": vol, "leaf": leaf, "norm_title": nt,
                    "n_chocr": n_choc, "n_ours": n_ours,
                    "chocr_titles": chocr_titles[nt],
                    "our_titles": sorted(t for t in leaf_titles[(vol, leaf)]
                                         if normalize_title(t) == nt),
                })

    candidates.sort(key=lambda c: -(c["n_chocr"] - c["n_ours"]))
    print(f"Total leaves scanned: {len(leaves)}")
    print(f"Candidate (vol, leaf, title) with missing homonyms: {len(candidates)}")
    print()
    print(f"{'vol':<5} {'leaf':<6} {'title':<35} {'chocr':>5} {'ours':>5}")
    print("-" * 70)
    for c in candidates[:40]:
        print(f"{c['vol']:<5} {c['leaf']:<6} {c['norm_title'][:33]:<35} "
              f"{c['n_chocr']:>5} {c['n_ours']:>5}")
    if len(candidates) > 40:
        print(f"... +{len(candidates)-40} more")

    if args.html:
        OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
        rows_html = []
        for c in candidates:
            ia = (f"https://archive.org/details/diccionariogeogr{c['vol']}mado"
                  f"/page/n{c['leaf']}/mode/2up")
            chocr_html = "<br>".join(escape(t) for t in c["chocr_titles"])
            ours_html = "<br>".join(escape(t) for t in c["our_titles"]) or "—"
            diff = c["n_chocr"] - c["n_ours"]
            band = "band-red" if diff >= 3 else "band-orange" if diff == 2 else "band-yellow"
            rows_html.append(f"""
            <tr class="{band}">
              <td class="num">{diff:+d}</td>
              <td>{escape(c['norm_title'].upper())}</td>
              <td>tom {escape(c['vol'])} / leaf {c['leaf']}
                 · <a href="{ia}" target="_blank">facsímil ↗</a></td>
              <td>{c['n_chocr']}: {chocr_html}</td>
              <td>{c['n_ours']}: {ours_html}</td>
            </tr>""")
        html = f"""<!DOCTYPE html><html lang="ca"><head><meta charset="utf-8">
<title>Auditoria d'homònims perduts — Madoz</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 1.5em; max-width: 1300px; background: #fafaf6; }}
  h1 {{ color: #c14a2c; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; font-size: 0.92em; }}
  th {{ background: #f3f0e8; text-align: left; padding: 0.5em 0.7em; position: sticky; top: 0; }}
  td {{ padding: 0.5em 0.7em; vertical-align: top; border-bottom: 1px solid #f0eee8; }}
  td.num {{ text-align: center; font-family: ui-monospace, monospace; font-weight: 700; width: 4em; }}
  tr.band-red {{ background: #fff5f5; }} tr.band-red .num {{ color: #b00; }}
  tr.band-orange {{ background: #fffaf3; }} tr.band-orange .num {{ color: #c66; }}
  tr.band-yellow {{ background: #fffdf3; }} tr.band-yellow .num {{ color: #b8860b; }}
</style></head><body>
<h1>Homònims potencialment perduts</h1>
<p><strong>{len(candidates)}</strong> candidates (vol, leaf, títol) on el chocr té més articles balears que el nostre text_entries.</p>
<p>Heurística aproximada — pot tenir falsos positius. Cal verificar el facsímil per cada cas abans de recuperar.</p>
<table>
  <thead><tr><th class="num">+N</th><th>Títol</th><th>Localització</th>
    <th>Articles trobats al chocr</th><th>Entrades nostres</th></tr></thead>
  <tbody>{''.join(rows_html)}</tbody>
</table></body></html>"""
        OUT_HTML.write_text(html, encoding="utf-8")
        print(f"\nReport written: {OUT_HTML.relative_to(PROJECT)}")


if __name__ == "__main__":
    main()
