"""Apply curated text_entries → madoz_entries links for OCR variants
that the main linker can't catch automatically.

These are pairs where the OCR mangle is severe enough that even
parens-aware matching fails: BASEBA/BASERA (R→B), CUYEBA/CUYERA,
FROHA/FRORA (H from R), FUFOXA/FUFONA (X from N), VINAYELLA/VIÑA
VELLA, etc. Each entry is manually verified.

Run AFTER scripts/link_text_entries.py so it only fills the gaps the
main linker left unlinked. Re-runnable: only updates NULL rows.

  python scripts/apply_curated_links.py            # dry run
  python scripts/apply_curated_links.py --apply    # commit
"""
from pathlib import Path
import duckdb
import re
import sys
import unicodedata
from difflib import SequenceMatcher

PROJECT = Path(__file__).resolve().parent.parent
DB = str(PROJECT / "db" / "madoz.duckdb")
_DIGIT_FIX = str.maketrans({"1": "i", "0": "o", "5": "s", "8": "b"})


def normalize(title: str, aggressive: bool = False) -> str:
    if not title:
        return ""
    s = title.lower().strip()
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\s+[—\-]\s+.*$", " ", s)
    s = re.sub(r"\b(?:vulgo|antig?(?:\.|\b)|ant(?:\.|\b))\s+\S+", " ", s)
    s = re.sub(r"(?<=[a-z])([1058])(?=[a-z])",
               lambda m: m.group(1).translate(_DIGIT_FIX), s)
    s = re.sub(r"(?<=[a-z])([1058])$",
               lambda m: m.group(1).translate(_DIGIT_FIX), s)
    s = s.replace("v", "b").replace("ñ", "n")
    if aggressive:
        s = s.replace("x", "n").replace("h", "r")
        s = re.sub(r"(.)\1+", r"\1", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Manually curated whitelist of text_title → madoz_title.
# Each (text_title, madoz_title) pair represents an OCR variant the
# tighter automated rules also accept; the whitelist exists to make the
# audit explicit.
CURATED = {
    # agg-exact / agg-fuzzy: confident
    "FROHA (Son)": "FRORA (SON)",
    "FUFOXA": "FUFONA",
    "BORRAS (Son)": "BOBRAS (SON)",
    "ROTXNA": "ROTANA",
    "POLI.ENTIA": "POLLENTIA",
    "CARBONELL (Son)": "CARBONEI",
    "TOR DE GR AII": "TOR DE GRAU",
    "NADAL (sol": "NADAL (SO)",
    "OLIVER (soj": "OLIVER (SO)",
    "RUSINOL fso)": "RUSINOL (so)",
    "SALA(so": "SALA",
    "SALAS ^cax)": "SALAS",
    "SALOM (soi": "SALOM (so)",
    "SIMÓ ,'son)": "SIMÓ (SO)",
    "SIMÓ íso)": "SIMÓ (SO)",
    "TORRETA (lan": "TORRETA (LA)",
    "VERI (soN": "VERI (SO)",
    "ROSSIÑOL (so)": "RUSINOL (so)",
    # fuzzy-low: high-confidence OCR variants
    # LLUGALGARI → LLUCALCARI (the hamlet in Deyá; NOT LLUCALARI San
    # Antonio de, which is the nearby feligresía). Audited 2026-05-16.
    "LLUGALGARI": "LLUCALCARI",
    "VINAYELLA": "VIÑA VELLA",
    "BASEBA (Cana)": "BASERA (CANA)",
    "CUYEBA (so)": "CUYERA (SO)",
    "REYNES (so)": "REYNEA (So)",
    "SARAN1 (son)": "SARANI (Sos)",
    "AIREFLOR": "AYRE FLOR",
    "PEDREGARS": "PEDRERAS",
    "POU COLÜMEK": "POU COLOMER",
    "RUBIES": "RUBERS",
    # token-set with corroborating evidence (SERRA + (CAN) — OCR mangle of paren)
    "SERRA (i.\\": "SERRA (CAN)",
}


def main():
    apply = "--apply" in sys.argv
    con = duckdb.connect(DB, read_only=not apply)
    mrows = con.execute("SELECT id, title FROM madoz_entries").fetchall()
    mby_norm = {}
    mby_title = {}
    for mid, mt in mrows:
        mby_norm.setdefault(normalize(mt), []).append((mid, mt))
        mby_title[mt] = mid

    unmatched = con.execute(
        "SELECT id, vol, leaf, title FROM text_entries WHERE madoz_entry_id IS NULL ORDER BY title"
    ).fetchall()

    updates = []
    audit = []
    for tid, vol, leaf, t in unmatched:
        # 1. curated whitelist (case-sensitive on text_title to be deterministic)
        if t in CURATED:
            mt = CURATED[t]
            mid = mby_title.get(mt)
            if mid:
                updates.append((mid, tid))
                audit.append(("curated", t, mt))
                continue
            else:
                audit.append(("curated-target-not-found", t, mt))
                continue

        # 2. aggressive-normalized exact match — but skip if multiple
        # madoz_entries share the same normalized title (ambiguous; we
        # can't pick one). Also skip if the normalized title is too
        # short (<5 chars) — high collision risk.
        na = normalize(t, aggressive=True)
        if na and len(na) >= 5:
            candidates = [
                (mid, mt) for mid, mt in mrows
                if normalize(mt, aggressive=True) == na
            ]
            if len(candidates) == 1:
                # Extra guard: skip if the candidate is a generic
                # place-type article (single token, ≤8 chars).
                mid, mt = candidates[0]
                mt_norm = normalize(mt)
                if len(mt_norm.split()) > 1 or len(mt_norm) > 8:
                    updates.append((mid, tid))
                    audit.append(("agg-exact", t, mt))

    if apply:
        for mid, tid in updates:
            con.execute("UPDATE text_entries SET madoz_entry_id=? WHERE id=?", [mid, tid])

    print(f"Applied {len(updates)} updates" if apply else f"DRY RUN: {len(updates)} updates")
    for rule, tt, mt in audit:
        print(f"  {rule:<25} {tt[:35]:<35} → {mt}")


if __name__ == "__main__":
    main()
