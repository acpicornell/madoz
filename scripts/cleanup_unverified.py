"""Typographic OCR cleanup for the 32 'unverified' text_entries created
by rescue_unlinked.py (Phase 3, 4, 5).

Strict cleanup policy:
  - Fix unambiguous OCR artifacts: R→B in 'Raleares' / 'Paleares', glued
    abbreviations ('ayunt.en' → 'ayunt. en'), spurious '■' chars,
    Greek-letter / digit confusions inside known words ('I'oUenza' →
    'Pollenza', 'F1ÜL' → 'FIOL').
  - Preserve Madoz's own spellings verbatim (Santagny stays Santagny,
    Lluchmayor stays Lluchmayor, BENISALEM never becomes BINISALEM).
  - NEVER rewrite, paraphrase, or fill in cropped text. If a snippet
    ends mid-word ('felig. de Pollen-'), leave the truncation; the next
    pipeline can chase the continuation leaf.
  - Title corrections only when the canonical Madoz form is unambiguous
    from the body context and Mallorquí/Castilian toponymy.

Promotes confidence='unverified' → 'medium'. Idempotent (re-running on
an already-cleaned row is a no-op).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"


# ── Regex rules — applied in order to every description ───────────────

# Each rule is (pattern, replacement, description). Run in order.
RULES: list[tuple[str, str, str]] = [
    # Strip block / junk characters inserted by the OCR
    (r"[■▪●◆■]+", "", "junk block chars"),

    # Soft-hyphen stitches BETWEEN letters of one word (Menor- ca →
    # Menorca). Keep hyphens that are real (between separate words).
    (r"([a-záéíóúñ])-\s+([a-záéíóúñ])", r"\1\2", "intra-word hyphen+space"),

    # Glued 'do' / 'de' before a Balearic toponym → split
    (r"\bdoMallorca\b", "de Mallorca", "doMallorca"),
    (r"\bdoManacor\b", "de Manacor", "doManacor"),
    (r"\bdoPalma\b", "de Palma", "doPalma"),
    (r"\bdoRaleares\b", "de Baleares", "doRaleares"),
    (r"\bdo\s*Cartagena\b", "de Cartagena", "do Cartagena"),
    (r"\bdeSantagny\b", "de Santagny", "deSantagny"),  # Madoz spells it Santagny
    (r"\bdeSineu\b", "de Sineu", "deSineu"),
    (r"\bdeSelva\b", "de Selva", "deSelva"),
    (r"\bdeLlubi\b", "de Llubí", "deLlubi"),
    (r"\bdeLluchmayor\b", "de Lluchmayor", "deLluchmayor"),
    (r"\bdeManacor\b", "de Manacor", "deManacor"),
    (r"\bdeAlaró\b", "de Alaró", "deAlaró"),
    (r"\bdeCampanet\b", "de Campanet", "deCampanet"),
    (r"\bdeEspolias\b", "de Esporlas", "deEspolias"),  # OCR Esporlas → Espolias
    (r"\bdeCampos\b", "de Campos", "deCampos"),

    # 'Raleares' / 'Paleares' OCR mis-reads of Baleares (R/P→B)
    (r"\bRaleares\b", "Baleares", "Raleares→Baleares"),
    (r"\bPaleares\b", "Baleares", "Paleares→Baleares"),

    # Other common single-word OCR confusions
    (r"\bMalloica\b", "Mallorca", "Malloica→Mallorca"),
    (r"\bIbij\s*za\b", "Ibiza", "Ibij za→Ibiza"),
    (r"\bI'oUenza\b", "Pollenza", "I'oUenza→Pollenza"),
    (r"\bPoUenza\b", "Pollenza", "PoUenza→Pollenza"),

    # Glued abbreviations and openers (light touch — only known-safe joins)
    (r"\bayunt\.en\b", "ayunt. en", "ayunt.en→ayunt. en"),
    (r"\bavunt\.", "ayunt.", "avunt.→ayunt."),
    (r"\bjud\.de\b", "jud. de", "jud.de→jud. de"),
    (r"\bjud,\s*de\b", "jud. de", "jud,de→jud. de"),
    (r"\bdióc\.de\b", "dióc. de", "dióc.de→dióc. de"),
    (r"\bprov\.,\s*aud\.terr\b", "prov., aud. terr", "aud.terr→aud. terr"),
    (r"\bprov\.de\b", "prov. de", "prov.de→prov. de"),
    (r"\bMallorca,prov\b", "Mallorca, prov", "Mallorca,prov"),
    (r"\bBaleares,part\b", "Baleares, part", "Baleares,part"),

    # 'p:irt.' (colon for OCR misread of dot) → 'part.'
    (r"\bp:irt\.", "part.", "p:irt.→part."),
    (r"\bp\.irt\.", "part.", "p.irt.→part."),
    (r"\bparí\.", "part.", "parí.→part."),

    # 'lerm.', 'terra' → 'térm.' (OCR é→nothing or e→r)
    (r"\bterrn\b", "térm.", "terrn→térm."),
    (r"\blerm\.", "térm.", "lerm.→térm."),
    (r"\btkrji\b", "térm", "tkrji→térm"),  # heavy OCR

    # 'jurisd.' variants
    (r"\bjui\s*isd\.", "jurisd.", "jui isd.→jurisd."),
    (r"\bjui\s+isd\.", "jurisd.", "jui isd.→jurisd."),
    (r"\bjuiisd\.", "jurisd.", "juiisd.→jurisd."),
    (r"\bjunsd\.", "jurisd.", "junsd.→jurisd."),
    (r"\bjuriíd\.", "jurisd.", "juriíd.→jurisd."),
    (r"\bjurisdici\s*ion\b", "jurisdicción", "jurisdicion→jurisdicción"),

    # 'felig.' variants
    (r"\bíelig\.", "felig.", "íelig.→felig."),

    # Curated-mirror specific artifacts:
    # 'feligresiaenlaislaypartidojudicialydiócesis' → space-out
    (r"\bfeligresiaenlaislaypartidojudicialydi[óo]cesis\b",
     "feligresía en la isla, partido judicial y diócesis",
     "curated: glued feligresia... → spaced"),
    # Letter-spaced curated headings: 'S I T .' 'C L I M A' 'P O B L .'
    # 'R I Q U E Z A' — Madoz uses these as small-caps section markers;
    # we render them as their bare expansion.
    (r"\bS\s+I\s+T\s*\.", "SIT.", "letter-spaced SIT."),
    (r"\bC\s+L\s+I\s+M\s+A\b", "CLIMA", "letter-spaced CLIMA"),
    (r"\bP\s+O\s+B\s+L\s*\.", "POBL.", "letter-spaced POBL."),
    (r"\bR\s+I\s+Q\s+U\s+E\s+Z\s+A\b", "RIQUEZA", "letter-spaced RIQUEZA"),
    (r"\bP\s+R\s+O\s+D\s*\.", "PROD.", "letter-spaced PROD."),
    (r"\bC\s+O\s+N\s+T\s+R\s*\.", "CONTR.", "letter-spaced CONTR."),

    # ',•' or '.•' often a misread of ':' / period
    (r"[.,]\s*•", ":", ".• or ,• → :"),

    # 'ye. g.' for 'y c. g.' (OCR yc confusion)
    (r"\bye\.\s*g\.", "y c. g.", "ye. g.→y c. g."),
    # 'ta isla' for 'la isla' (t/l at word start)
    (r"\b(?<=en\s)ta\s+isla\b", "la isla", "ta isla→la isla"),
    # '(le ' for '(de ' (l/d confusion after open paren)
    (r"\(le\s+", "(de ", "(le →(de "),
    # 'podl.' → 'pobl.', 'puod.' → 'prod.' (l/d, u/r)
    (r"\bpodl\.", "pobl.", "podl.→pobl."),
    (r"\bpuod\.", "prod.", "puod.→prod."),
    # 'atd.' → 'ald.' (case-by-case OCR variation)
    (r"\batd\.", "ald.", "atd.→ald."),

    # Collapse run of >1 spaces (light, won't damage prose)
    (r"  +", " ", "double space"),
]


# ── Title corrections — explicit, conservative ────────────────────────

# id → (new_title, reason). Only for entries where the canonical Madoz
# form is unambiguous from the article body + Mallorquí toponymy.
TITLE_FIXES: dict[int, tuple[str, str]] = {
    9091: ("ARIANT", "AKL4NT body 'felig. de Pollen-' → ARIANT predio in Pollença"),
    9092: ("ARIANY", "ARIA5íV body 'jurisd. y felig. de Petra' → ARIANY (now Ariany)"),
    9093: ("BAJOS (cabo de)", "'HA SOS ÍCADO DE)' body 'cabo occidental de Menorca... Ciudadela'"),
    9095: ("BOSCH (can)", "BOSCII = BOSCH; II / H OCR confusion"),
    9096: ("FIOL (son)", "F1ÜL (son) — 1→I, Ü→O common OCR"),
    9098: ("LLENAIRE", "LLE.\\An\\E body 'predio... Pollenza' → LLENAIRE"),
    9099: ("LLINAS", "LLI.NWS = LLINAS (verified vs PDF with Tesseract spa: Madoz prints LLINAS without R)"),
    9100: ("LLINAS ó MOLINS DE LLINAS", "LLIN.\\S ó .MOUNS DE LLIXAS = LLINAS ó MOLINS DE LLINAS (Tesseract-verified)"),
    9101: ("LLOBACH", "LLOBACII II→H"),
    9102: ("PEDRUXELLA (Gran), (antig. Pertuxella)", "PEDRÜXELLA (cnAN)"),
    9103: ("PERPIÑA (so)", "PERPlHA (so) lH→ÑA"),
    9105: ("RAMIS (Son)", "IUMIS (Son = RAMIS (Son) — Tesseract-verified vs PDF; canonical Mallorquí family name"),
    9107: ("RIPOLL", "already RIPOLL, no change"),  # placeholder, noop
}


def clean_description(text: str, lemma_swap: tuple[str, str] | None = None) -> str:
    """Apply OCR cleanup. If `lemma_swap` is (old_lemma, new_lemma), also
    replace the article-opening lemma (which appears at the start of the
    description with the same OCR mangle as the title)."""
    if not text:
        return text
    out = text
    if lemma_swap:
        old, new = lemma_swap
        # Old lemma may appear at start followed by ':', '.,', '.•', etc.
        # Replace only the leading instance to preserve any in-body refs.
        out = re.sub(
            rf"^\s*{re.escape(old)}(?=\s*[\.,:•])",
            new,
            out,
            count=1,
        )
    for pat, repl, _ in RULES:
        out = re.sub(pat, repl, out)
    return out.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Commit changes. Default is dry-run.")
    ap.add_argument("--show-diff", action="store_true",
                    help="Print before/after for every changed row.")
    args = ap.parse_args()

    if not DB.exists():
        sys.exit(f"DB not found: {DB}")

    con = duckdb.connect(str(DB), read_only=not args.apply)

    rows = con.execute("""
        SELECT id, title, description
        FROM text_entries
        WHERE confidence = 'unverified'
        ORDER BY id
    """).fetchall()
    print(f"=== {len(rows)} unverified entries to process ===\n")

    changes = []
    title_changes = 0
    desc_changes = 0
    for tid, title, desc in rows:
        new_title = title
        lemma_swap = None
        if tid in TITLE_FIXES and TITLE_FIXES[tid][0] != title:
            new_title = TITLE_FIXES[tid][0]
            title_changes += 1
            # The body's article opener carries the same OCR mangle as
            # the title; swap it too. Strip parens content from title for
            # lemma matching since the body opener uses the bare lemma.
            old_bare = re.sub(r"\([^)]*\)", "", title).strip()
            new_bare = re.sub(r"\([^)]*\)", "", new_title).strip()
            if old_bare and new_bare and old_bare != new_bare:
                lemma_swap = (old_bare, new_bare)
        new_desc = clean_description(desc, lemma_swap=lemma_swap)
        if new_desc != desc:
            desc_changes += 1
        if new_desc != desc or new_title != title:
            changes.append((tid, title, new_title, desc, new_desc))

    print(f"Rows with description changes: {desc_changes}")
    print(f"Rows with title    changes:   {title_changes}\n")

    if args.show_diff:
        for tid, ot, nt, od, nd in changes:
            print(f"--- id={tid} ---")
            if ot != nt:
                print(f"  title:  {ot!r}")
                print(f"      → {nt!r}")
            if od != nd:
                # Show a unified-style diff of description
                from difflib import unified_diff
                diff = list(unified_diff(
                    od.splitlines(keepends=True),
                    nd.splitlines(keepends=True),
                    lineterm="", n=1, fromfile="before", tofile="after",
                ))
                if diff:
                    for line in diff[:30]:
                        print(f"    {line.rstrip()}")
            print()

    if args.apply and changes:
        for tid, _, nt, _, nd in changes:
            con.execute(
                """UPDATE text_entries
                   SET title = ?, description = ?, confidence = 'medium'
                   WHERE id = ?""",
                [nt, nd, tid],
            )
        # Also promote any unverified row that had NO changes — they're
        # still cleaner than before (regex left them untouched because
        # they were already clean). Bump to medium.
        con.execute(
            """UPDATE text_entries
               SET confidence = 'medium'
               WHERE confidence = 'unverified'"""
        )
        print(f"\n=== APPLIED: {len(changes)} rows updated, all 'unverified' → 'medium' ===")
    else:
        print(f"=== DRY-RUN: {len(changes)} rows would change ===")


if __name__ == "__main__":
    main()
