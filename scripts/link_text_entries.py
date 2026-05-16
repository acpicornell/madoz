"""Cross-link text_entries to chocr_entries and madoz_entries by fuzzy title match.

For each row in text_entries, fill:
  - chocr_entry_id: best title match on the same (vol, leaf) in chocr_entries
  - madoz_entry_id: best title match across all madoz_entries

Normalization handles common OCR mangles:
  - Ñ → N, accents stripped
  - B ↔ V → B (e.g. VANALHUFAR ≈ BANALBUFAR)
  - OCR digit confusions inside words: 1→i, 0→o, 5→s, 8→b
    (FELAN1TX → FELANITX)
  - Parens stripped; punctuation, hyphens collapsed; lowercase

Thresholds:
  - chocr: 0.60 (candidate set is small, same leaf)
  - madoz: 0.85 (1k+ candidates, must be strict)

Re-runnable; only updates NULL columns.
"""
from __future__ import annotations

import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"

# OCR digit→letter confusions inside words (not at start)
_DIGIT_FIX = str.maketrans({"1": "i", "0": "o", "5": "s", "8": "b"})


def normalize(title: str) -> str:
    if not title:
        return ""
    s = title.lower().strip()
    # strip accents
    s = "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )
    # strip parenthesized / bracketed suffixes/prefixes
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    # OCR digit fixes only when adjacent to letters
    s = re.sub(
        r"(?<=[a-z])([105 8])(?=[a-z])",
        lambda m: m.group(1).translate(_DIGIT_FIX),
        s,
    )
    s = re.sub(
        r"(?<=[a-z])([105 8])$",
        lambda m: m.group(1).translate(_DIGIT_FIX),
        s,
    )
    # B↔V canonicalize
    s = s.replace("v", "b")
    # Ñ→N (already handled by accent strip but defensive)
    s = s.replace("ñ", "n")
    # remove hyphens/commas/dots/etc
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def link_chocr(con: duckdb.DuckDBPyConnection, threshold: float = 0.85) -> tuple[int, list]:
    """Match text_entries to chocr_entries on same (vol, leaf).

    Strategy:
      1. Exact normalized match → assign.
      2. text-title startswith chocr-title (or vice versa) → assign.
         Catches mega sub-entries: "ALAYOR (peñas y atalaya)" → "ALAYOR".
      3. Fuzzy ≥ threshold → assign. Catches chocr OCR mangles
         ("ALC ARIAS" → "ALCARIAS", "LLÜÍVIE'NA" → "LLUMENA").
    Many-to-one is allowed: one chocr index entry may legitimately back
    several text entries (mega articles split into c. + part. jud.).
    """
    rows = con.execute(
        """SELECT id, vol, leaf, title FROM text_entries
           WHERE chocr_entry_id IS NULL"""
    ).fetchall()

    updates: list[tuple[int, int]] = []
    misses: list[tuple] = []
    for tid, vol, leaf, title in rows:
        ntitle = normalize(title)
        cands = con.execute(
            "SELECT id, title FROM chocr_entries WHERE vol=? AND leaf=?",
            [vol, leaf],
        ).fetchall()
        if not cands:
            misses.append((tid, vol, leaf, title, "no chocr on this leaf"))
            continue

        chosen = None
        # 1. exact
        for cid, ct in cands:
            if normalize(ct) == ntitle:
                chosen = (cid, ct, 1.0)
                break
        # 2. startswith
        if not chosen:
            for cid, ct in cands:
                nct = normalize(ct)
                if not nct or not ntitle:
                    continue
                if ntitle.startswith(nct + " ") or nct.startswith(ntitle + " "):
                    chosen = (cid, ct, 0.99)
                    break
        # 3. fuzzy
        if not chosen:
            scored = sorted(
                ((similarity(ntitle, normalize(ct)), cid, ct) for cid, ct in cands),
                reverse=True,
            )
            if scored and scored[0][0] >= threshold:
                chosen = (scored[0][1], scored[0][2], scored[0][0])

        if chosen:
            updates.append((chosen[0], tid))
        else:
            best = max(
                ((similarity(ntitle, normalize(ct)), ct) for cid, ct in cands),
                default=(0, None),
            )
            misses.append((tid, vol, leaf, title, f"best={best[1]!r}@{best[0]:.2f}"))

    if updates:
        con.executemany(
            "UPDATE text_entries SET chocr_entry_id=? WHERE id=?", updates
        )
    return len(updates), misses


def _normalize_full(title: str) -> str:
    """Normalize while *preserving* parenthetical content. Critical for
    disambiguating multi-form predios: 'BLAY (Son)' vs 'BLAY (Can)',
    'ADAYA (islas de)' vs 'ADAYA (granja de)' — bare-name match alone
    arbitrarily picks one and silently mis-links the other.
    """
    if not title:
        return ""
    s = title.lower().strip()
    s = "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )
    s = re.sub(
        r"(?<=[a-z])([105 8])(?=[a-z])",
        lambda m: m.group(1).translate(_DIGIT_FIX), s,
    )
    s = s.replace("v", "b").replace("ñ", "n")
    s = re.sub(r"[^a-z0-9() ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_parens(title: str) -> str:
    """Extract and normalize the parenthetical content of a title."""
    if not title:
        return ""
    parts = re.findall(r"\(([^)]+)\)", title)
    if not parts:
        return ""
    s = "".join(
        c for c in unicodedata.normalize("NFD", " ".join(parts).lower())
        if unicodedata.category(c) != "Mn"
    )
    s = s.replace("v", "b").replace("ñ", "n")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def link_madoz(con: duckdb.DuckDBPyConnection, threshold: float = 0.85) -> tuple[int, list]:
    """Match text_entries to madoz_entries by title.

    Pass order (highest priority first):
      1. Exact full-title (parens preserved, case/accent/V↔B normalized)
      2. Bare-name match disambiguated by parens-content fuzzy similarity
         (when multiple madoz_entries share a bare name, pick the one
         whose parens content best matches the text-entry's parens).
      3. Bare-name match when only ONE candidate exists.
      4. Fuzzy similarity ≥ threshold on bare-name as last resort.

    No (vol, leaf) constraint — the WordPress mirror doesn't track those.
    Many predios are absent from the mirror entirely, so plenty of
    legitimate misses (left unlinked rather than force-matched).
    """
    rows = con.execute(
        """SELECT id, title FROM text_entries WHERE madoz_entry_id IS NULL"""
    ).fetchall()
    mrows = con.execute("SELECT id, title FROM madoz_entries").fetchall()

    # Pre-compute madoz normalizations and indexes
    madoz_idx = []  # (mid, mt, n_full, n_bare, n_parens)
    by_full: dict[str, list[int]] = {}
    by_bare: dict[str, list[int]] = {}
    for mid, mt in mrows:
        f = _normalize_full(mt)
        b = normalize(mt)
        p = _normalize_parens(mt)
        madoz_idx.append((mid, mt, f, b, p))
        by_full.setdefault(f, []).append(mid)
        by_bare.setdefault(b, []).append(mid)
    madoz_by_id = {mid: (mt, f, b, p) for mid, mt, f, b, p in madoz_idx}

    updates: list[tuple[int, int]] = []
    misses: list[tuple] = []
    for tid, title in rows:
        tf = _normalize_full(title)
        tb = normalize(title)
        tp = _normalize_parens(title)
        if not tb:
            continue
        chosen = None
        tried_bare = False

        # 1. exact full-title match
        if tf and tf in by_full and by_full[tf]:
            chosen = by_full[tf][0]

        # 2 & 3. bare-name match with parens disambiguation
        if chosen is None and tb in by_bare:
            tried_bare = True
            cands = by_bare[tb]
            if len(cands) == 1:
                chosen = cands[0]
            else:
                # Score by parens-content similarity
                best = None
                for mid in cands:
                    mp = madoz_by_id[mid][3]
                    if tp and mp:
                        s = similarity(tp, mp)
                        if best is None or s > best[1]:
                            best = (mid, s)
                    elif not tp and not mp:
                        if best is None or 1.0 > best[1]:
                            best = (mid, 1.0)
                if best and best[1] >= 0.5:
                    chosen = best[0]
                elif not tp:
                    # text has no parens — prefer the candidate with no parens
                    for mid in cands:
                        if not madoz_by_id[mid][3]:
                            chosen = mid
                            break
                # If text has parens but no madoz candidate had matching
                # parens content (≥0.5), leave unlinked — picking an
                # arbitrary same-bare candidate would mis-link
                # ('MALLORCA (diócesis)' → 'MALLORCA (Capitania general)',
                # 'COVAS (las)' → 'COVAS (SO)', etc.).

        # 4. fuzzy on bare name — but NOT if we already tried bare-match
        # with multiple parens-mismatched candidates (that's a real
        # ambiguity we explicitly chose to leave unlinked; falling back
        # to fuzzy here would just re-pick one of those same candidates).
        if chosen is None and not tried_bare:
            scored = sorted(
                ((similarity(tb, b), mid, mt)
                 for mid, mt, f, b, p in madoz_idx),
                reverse=True,
            )
            if scored and scored[0][0] >= threshold:
                chosen = scored[0][1]

        if chosen is not None:
            updates.append((chosen, tid))
        else:
            best = max(
                ((similarity(tb, b), mt) for mid, mt, f, b, p in madoz_idx),
                default=(0, None),
            )
            misses.append((tid, title, f"best={best[1]!r}@{best[0]:.2f}"))

    if updates:
        con.executemany(
            "UPDATE text_entries SET madoz_entry_id=? WHERE id=?", updates
        )
    return len(updates), misses


def main() -> None:
    if not DB.exists():
        sys.exit(f"DB not found at {DB}.")
    con = duckdb.connect(str(DB))
    print("Linking text_entries → chocr_entries (same vol/leaf)...")
    nc, miss_c = link_chocr(con)
    print(f"  matched {nc} rows; {len(miss_c)} unmatched")
    for m in miss_c[:20]:
        print(f"    miss: tom{m[1]} leaf {m[2]}  {m[3]!r}  ({m[4]})")
    if len(miss_c) > 20:
        print(f"    ... and {len(miss_c)-20} more")

    print()
    print("Linking text_entries → madoz_entries (global title)...")
    nm, miss_m = link_madoz(con)
    print(f"  matched {nm} rows; {len(miss_m)} unmatched (likely no entry in mirror)")
    for m in miss_m[:20]:
        print(f"    miss: {m[1]!r}  ({m[2]})")
    if len(miss_m) > 20:
        print(f"    ... and {len(miss_m)-20} more")

    print()
    # Summary
    n = con.execute("SELECT COUNT(*) FROM text_entries").fetchone()[0]
    nc_total = con.execute(
        "SELECT COUNT(*) FROM text_entries WHERE chocr_entry_id IS NOT NULL"
    ).fetchone()[0]
    nm_total = con.execute(
        "SELECT COUNT(*) FROM text_entries WHERE madoz_entry_id IS NOT NULL"
    ).fetchone()[0]
    print(f"Final: {n} text_entries  |  chocr={nc_total}  |  madoz={nm_total}")


if __name__ == "__main__":
    main()
