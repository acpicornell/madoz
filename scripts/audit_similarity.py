"""Compare every (text_entries, madoz_entries) linked pair and produce
an HTML report flagging the low-similarity ones for manual review.

Quality signal: if our OCR description and diccionariomadoz.com's
content_text describe the same place, the normalized similarity should
be ≥0.7. Below that, possibilities are:

  a) Multi-page mega-article where our pipeline only captured a fragment
     (PALMA, IBIZA, MAHON spread across 3-4 leaves).
  b) Long village article (Felanitx, Sóller, …) where our OCR has only
     the intro paragraph and theirs has the full body.
  c) Genuine mis-link from the fuzzy title matcher (homonyms across
     islands or peninsular).

The report lets a human eyeball each case and decide.

  python scripts/audit_similarity.py            # writes data/reports/similarity_audit.html
  python scripts/audit_similarity.py --threshold 0.5
"""
from __future__ import annotations
import argparse
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from html import escape
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
OUT_HTML = PROJECT / "data" / "reports" / "similarity_audit.html"


def normalize(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>|&[a-z]+;|&#\d+;", " ", s)
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--threshold", type=float, default=0.7,
                    help="report pairs with similarity below this (default 0.7)")
    ap.add_argument("--all-pairs", action="store_true",
                    help=("show every flagged pair (default keeps only the ones "
                          "where our title differs from theirs — i.e. likely "
                          "mis-links rather than just-partial-OCR captures)"))
    args = ap.parse_args()

    con = duckdb.connect(str(DB), read_only=True)
    rows = con.execute("""
        SELECT t.id, t.vol, t.leaf, t.page_printed, t.title,
               t.description, t.island, t.judicial_district, t.municipality,
               m.id AS mid, m.title AS mtit, m.url AS murl, m.content_text
        FROM text_entries t
        JOIN madoz_entries m ON m.id = t.madoz_entry_id
        WHERE t.description IS NOT NULL AND m.content_text IS NOT NULL
    """).fetchall()

    link_count = Counter(r[9] for r in rows)

    pairs = []
    for (tid, vol, leaf, page, title, desc, isl, dist, muni,
         mid, mtit, murl, content) in rows:
        a, b = normalize(desc), normalize(content)
        if not a or not b:
            continue
        ratio = SequenceMatcher(None, a, b).ratio()
        pairs.append({
            "tid": tid, "vol": vol, "leaf": leaf, "page": page,
            "title": title, "desc": desc, "len_ours": len(a),
            "island": isl, "district": dist, "municipality": muni,
            "mid": mid, "mtit": mtit, "murl": murl,
            "content": content, "len_theirs": len(b),
            "ratio": ratio,
            "is_mega": link_count[mid] > 1,
        })

    pairs.sort(key=lambda p: p["ratio"])
    flagged = [p for p in pairs if p["ratio"] < args.threshold]

    # By default only keep pairs whose titles differ — the same-title
    # rows are overwhelmingly "long village article on their side, intro
    # fragment on ours" (correct link, just length asymmetry) and
    # they're noise for a mis-link audit. Pass --all-pairs to undo.
    if not args.all_pairs:
        before = len(flagged)
        flagged = [
            p for p in flagged
            if normalize(p["title"]) != normalize(p["mtit"])
        ]
        print(f"\nFilter «titles differ»: {before} → {len(flagged)} pairs kept "
              f"(pass --all-pairs to see all flagged).")

    # Stats
    bins = {"<0.3": 0, "0.3-0.5": 0, "0.5-0.7": 0, "0.7-0.9": 0, "≥0.9": 0}
    for p in pairs:
        r = p["ratio"]
        if r < 0.3: bins["<0.3"] += 1
        elif r < 0.5: bins["0.3-0.5"] += 1
        elif r < 0.7: bins["0.5-0.7"] += 1
        elif r < 0.9: bins["0.7-0.9"] += 1
        else: bins["≥0.9"] += 1

    print(f"Total linked pairs: {len(pairs)}")
    for k, v in bins.items():
        pct = 100 * v / len(pairs)
        print(f"  {k:<8} {v:5}  ({pct:5.1f}%)")
    print(f"\nFlagged (<{args.threshold}): {len(flagged)} pairs")
    mega = sum(1 for p in flagged if p["is_mega"])
    print(f"  mega-article fragments: {mega}")
    print(f"  one-to-one (more likely real divergence): {len(flagged) - mega}")

    # Compact table: just the two titles side by side so you can spot
    # bad pairings at a glance. Sim + flags as small badges.
    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    rows_html = []
    for p in flagged:
        r = p["ratio"]
        band = (
            "band-red" if r < 0.3 else
            "band-orange" if r < 0.5 else
            "band-yellow" if r < 0.7 else "band-ok"
        )
        flag_bits = []
        if p["is_mega"]:
            flag_bits.append('<span class="flag flag-mega" title="múltiples text_entries enllaçats al mateix madoz_entry">M</span>')
        if p["len_theirs"] > 3 * max(p["len_ours"], 1):
            flag_bits.append('<span class="flag flag-short" title="nostre OCR és &lt;⅓ de la seva longitud">N</span>')
        if p["len_ours"] > 3 * max(p["len_theirs"], 1):
            flag_bits.append('<span class="flag flag-short" title="el seu és &lt;⅓ del nostre">S</span>')
        flags_html = " ".join(flag_bits)
        ia = (f'https://archive.org/details/diccionariogeogr{p["vol"]}mado'
              f'/page/n{p["leaf"]}/mode/2up')
        murl = escape(p["murl"] or "")
        rows_html.append(f"""
        <tr class="{band}">
          <td class="sim">{r:.2f}</td>
          <td class="ours"><a href="{ia}" target="_blank">{escape(p['title'])}</a>
            <span class="ctx">{escape(p['island'] or '—')} · tom {escape(p['vol'])}/{p['leaf']}</span></td>
          <td class="theirs"><a href="{murl}" target="_blank">{escape(p['mtit'])}</a></td>
          <td class="flags">{flags_html}</td>
        </tr>""")

    html = f"""<!DOCTYPE html>
<html lang="ca"><head><meta charset="utf-8">
<title>Auditoria de parelles — Madoz</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 1.5em; max-width: 1100px; color: #222; background: #fafaf6; }}
  h1 {{ color: #c14a2c; margin: 0 0 0.3em; }}
  .summary {{ background: #fff; border: 1px solid #ddd; padding: 0.8em 1.2em; border-radius: 6px; margin-bottom: 1.5em; }}
  .summary code {{ background: #f3f0e8; padding: 1px 4px; border-radius: 3px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; font-size: 0.94em; }}
  th {{ background: #f3f0e8; text-align: left; padding: 0.5em 0.7em; border-bottom: 1px solid #ddd; position: sticky; top: 0; }}
  td {{ padding: 0.45em 0.7em; border-bottom: 1px solid #f0eee8; }}
  td.sim {{ font-family: ui-monospace, monospace; text-align: center; font-weight: 700; width: 4em; }}
  td.ours, td.theirs {{ width: 42%; }}
  td.ours a, td.theirs a {{ color: #222; text-decoration: none; font-weight: 500; }}
  td.ours a:hover, td.theirs a:hover {{ color: #c14a2c; text-decoration: underline; }}
  td.ctx, td.ours .ctx {{ display: block; font-size: 0.78em; color: #888; margin-top: 2px; font-weight: normal; }}
  td.flags {{ width: 4em; }}
  tr.band-red td.sim {{ color: #b00; }}
  tr.band-red {{ background: #fff5f5; }}
  tr.band-orange td.sim {{ color: #c66; }}
  tr.band-orange {{ background: #fffaf3; }}
  tr.band-yellow td.sim {{ color: #b8860b; }}
  .flag {{ display: inline-block; width: 1.2em; height: 1.2em; line-height: 1.2em; text-align: center;
           font-size: 0.78em; border-radius: 3px; font-weight: 700; margin-right: 2px; }}
  .flag-mega {{ background: #fff3cd; color: #856404; }}
  .flag-short {{ background: #d1ecf1; color: #0c5460; }}
</style>
</head><body>
<h1>Auditoria de parelles — Madoz</h1>
<div class="summary">
  <p><strong>{len(pairs)}</strong> parelles enllaçades (text_entries ↔ madoz_entries),
     filtrades a <code>sim &lt; {args.threshold}</code> = <strong>{len(flagged)}</strong> per revisar
     (ordenades de pitjor a millor similaritat).</p>
  <p>Distribució global:
    <code>&lt;0.3</code>: {bins['<0.3']} ·
    <code>0.3–0.5</code>: {bins['0.3-0.5']} ·
    <code>0.5–0.7</code>: {bins['0.5-0.7']} ·
    <code>0.7–0.9</code>: {bins['0.7-0.9']} ·
    <code>≥0.9</code>: {bins['≥0.9']}
  </p>
  <p>Banderes: <span class="flag flag-mega">M</span> mega-article (múltiples nostres → 1 seu) ·
     <span class="flag flag-short">N</span> nostre &lt;⅓ del seu (captura parcial nostra) ·
     <span class="flag flag-short">S</span> seu &lt;⅓ del nostre. Clica un títol per obrir-ne la font.</p>
</div>
<table>
  <thead><tr>
    <th class="sim">Sim</th>
    <th>El nostre títol (→ facsímil IA)</th>
    <th>diccionariomadoz.com</th>
    <th></th>
  </tr></thead>
  <tbody>{''.join(rows_html)}</tbody>
</table>
</body></html>
"""
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"\nReport written to: {OUT_HTML.relative_to(PROJECT)}")
    print(f"  Open with: open {OUT_HTML}")


if __name__ == "__main__":
    main()
