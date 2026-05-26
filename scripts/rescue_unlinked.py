"""Rescue text_entries ↔ madoz_entries links that the main linker missed.

Three phases, each gated by --apply:

  1. Fill NULL madoz_entry_id where text title + place_type
     unambiguously matches a curated entry. Handles the multi-sense case
     the main linker can't disambiguate (MALLORCA provincia vs audiencia
     vs diócesis — title bare-name collides; place_type breaks the tie).

  2. Relink wrong-sense FKs: text_entry currently linked to a madoz_entry
     whose place_type clashes with the text_entry's place_type, AND a
     better candidate (same bare-lemma, matching place_type) exists.
     Only relinks when target is unambiguous.

  3. Promote chocr-only Balearic articles into text_entries: the chocr
     paragraph segmenter found them but Phase 1 of the main pipeline
     dropped them as non-Balearic. They survive in chocr_entries with
     madoz_entry_id=NULL, and a curated-mirror entry confirms they ARE
     Balearic. Insert a new text_entries row with the chocr OCR snippet
     as description (confidence='unverified', model='chocr-snippet').

Run dry first, then with --apply.
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import duckdb

PROJECT = Path(__file__).resolve().parent.parent
DB = PROJECT / "db" / "madoz.duckdb"
CHOCR_DIR = PROJECT / "data" / "text" / "_chocr"

# Castilian-form ↔ project-canonical form. Madoz uses Castilian spelling
# (DEVA, BAYOLI); our text_entries sometimes use the Catalan form (DEYA).
# Canonical lemma → list of alias forms. Used post-core_lemma(), so the
# keys/values must match what core_lemma() produces (V → B, accent strip,
# all-caps, no whitespace).
# DEVA (Madoz Castilian) ↔ DEYA (Catalan, the form our text_entries use)
# Both normalize via V→B: DEVA → DEBA, DEYA → DEYA. So the alias maps
# DEBA ↔ DEYA.
LEMMA_ALIAS = {
    "DEBA": "DEYA",
    "DEYA": "DEBA",
}

# Tokens that confirm a text body is talking about the Balearic Islands.
# Used in Phase 3 to reject curated false positives (e.g. ALCORNOCAL, an
# arroyo in Badajoz that the curated mirror mis-categorised as Balearic).
BALEARIC_TOKENS = re.compile(
    r"\b(?:Mallorca|Menorca|Iviza|Ibiza|Baleares|Cabrera|Formentera|"
    r"Palma\s+de\s+Mallorca|Mah[óo]n|Ciudadela|Eivissa)\b",
    re.IGNORECASE,
)

# Stricter set: 'Cabrera' alone is a peninsular collision (Cáceres sierra
# de la Cabrera, etc.). Require one of these for unambiguous Balearic
# classification. Used only when 'Cabrera' is the only signal.
BALEARIC_STRONG = re.compile(
    r"\b(?:Mallorca|Menorca|Iviza|Ibiza|Baleares|Mah[óo]n|Eivissa|"
    r"Palma\s+de\s+Mallorca|Formentera)\b",
    re.IGNORECASE,
)


_BALEARIC_TOKENS_COMPRESSED = re.compile(
    r"(?:mallorca|menorca|iviza|ibiza|baleares|cabrera|formentera|"
    r"palmademallorca|mahon|ciudadela|eivissa)"
)
_BALEARIC_STRONG_COMPRESSED = re.compile(
    r"(?:mallorca|menorca|iviza|ibiza|baleares|mahon|eivissa|"
    r"palmademallorca|formentera)"
)


def is_balearic_text(text: str) -> bool:
    """True iff the body unambiguously describes a Balearic place.

    Rejects peninsular articles that mention 'Cabrera' in passing
    (sierra de la Cabrera in Cáceres, etc.) and peninsular cross-refs
    that name a Balearic suffragan (VALENCIA arzobispado mentioning
    Mallorca and Menorca as dependencies).
    """
    if not text:
        return False
    # Collapse OCR junk between letters of a word: 'Menor- ■ca' must
    # still match 'Menorca'. Lowercase, strip accents, drop every
    # non-letter — then search the compressed forms.
    compressed = _strip_accents(text).lower()
    compressed = re.sub(r"[^a-z]+", "", compressed)
    if not _BALEARIC_TOKENS_COMPRESSED.search(compressed):
        return False
    if not _BALEARIC_STRONG_COMPRESSED.search(compressed):
        return False
    # Peninsular cross-ref filter: opener says 'en la prov. de <peninsular>'
    # but the article is about that province, not Mallorca/Menorca/Ibiza.
    # The opener appears in the first ~80 chars.
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

# Map of opening abbreviations Madoz uses to a normalized place_type.
# Used to recover place_type from chocr context when chocr_entries.place_type
# is NULL, so Phase 3 can disambiguate between same-lemma curated senses
# (SALINAS cabo vs SALINAS aldea).
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
    (re.compile(r"^\s*puerto\b|^\s*puerto?\b", re.I), "puerto"),
]


def infer_place_type(context_or_para: str | None) -> str | None:
    """Read the place_type from the opening of an article body."""
    if not context_or_para:
        return None
    # Strip leading title + separator (`LEMMA : body...`)
    s = re.sub(r"^[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ.\s\(\)\-,]{0,80}?[:.,]\s*", "", context_or_para)
    for pat, pt in PT_OPENERS:
        if pat.match(s):
            return pt
    return None


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def core_lemma(title: str) -> str:
    """Normalize a title to its bare lemma for matching.

    Strips parentheticals/brackets, trailing qualifiers ('vulgo X', '— X'),
    accents, B↔V, Ñ→N, common Saint prefixes, articles, and punctuation.
    """
    if not title:
        return ""
    s = title.upper().strip()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\s+[—\-]\s+.*$", " ", s)
    # Drop trailing 'VULGO X', 'ANT(IGUAMENTE) X' name variants
    s = re.sub(r"\bVULGO\b.*$", " ", s)
    s = re.sub(r"\bANT(?:IGUO|IGUAMENTE|IG\.|\.)\b.*$", " ", s)
    s = _strip_accents(s)
    s = re.sub(r"\b(SANTA|SANTO|SAN|SO|SON|LA|LAS|LOS|EL)\b", " ", s)
    s = s.replace("V", "B").replace("Ñ", "N")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def pt_aligned(text_pt: str | None, madoz_pt: str | None) -> int:
    """Score how well two place_type strings align (0..3).

    3 = exact, 2 = one is prefix of other, 1 = same first token,
    0 = mismatch. None on either side returns 0 (no info, can't trust).
    """
    if not text_pt or not madoz_pt:
        return 0
    tp = text_pt.lower().strip()
    mp = madoz_pt.lower().strip()
    if tp == mp:
        return 3
    if tp.startswith(mp) or mp.startswith(tp):
        return 2
    if tp.split()[0] == mp.split()[0]:
        return 1
    return 0


def phase1_fill_nulls(con: duckdb.DuckDBPyConnection, *, apply: bool) -> list[dict]:
    """Fill NULL madoz_entry_id by core-lemma + place_type matching.

    Conservative: only links when there's a place-type-aligned candidate
    that is itself unambiguous (no other candidate with same lemma + pt
    score). Skips ambiguous cases.
    """
    text_rows = con.execute("""
        SELECT id, title, place_type FROM text_entries
        WHERE madoz_entry_id IS NULL
    """).fetchall()
    madoz_rows = con.execute("""
        SELECT id, title, place_type FROM madoz_entries
    """).fetchall()

    # Index madoz_entries by core_lemma (and by aliased lemma)
    by_lemma: dict[str, list[tuple[int, str, str | None]]] = {}
    for mid, mt, mpt in madoz_rows:
        cl = core_lemma(mt)
        if cl:
            by_lemma.setdefault(cl, []).append((mid, mt, mpt))

    proposed = []
    for tid, ttitle, tpt in text_rows:
        cl = core_lemma(ttitle)
        candidates = list(by_lemma.get(cl, []))
        # Try alias
        alias = LEMMA_ALIAS.get(cl)
        if alias:
            candidates.extend(by_lemma.get(alias, []))
        if not candidates:
            continue
        # Score by place_type alignment
        scored = sorted(
            ((pt_aligned(tpt, mpt), mid, mt, mpt) for mid, mt, mpt in candidates),
            reverse=True,
        )
        top_score = scored[0][0]
        top_candidates = [s for s in scored if s[0] == top_score]
        if len(top_candidates) == 1 and top_score >= 1:
            _, mid, mt, mpt = top_candidates[0]
            proposed.append({
                "phase": 1, "tid": tid, "ttitle": ttitle, "tpt": tpt,
                "mid": mid, "mt": mt, "mpt": mpt, "score": top_score,
            })
        elif len(top_candidates) == 1 and top_score == 0 and len(candidates) == 1:
            # Only candidate, but no place_type info on either side.
            # Accept anyway — this is the common "single curated entry,
            # no place_type metadata" case (most curated stubs).
            _, mid, mt, mpt = top_candidates[0]
            proposed.append({
                "phase": 1, "tid": tid, "ttitle": ttitle, "tpt": tpt,
                "mid": mid, "mt": mt, "mpt": mpt, "score": 0,
            })

    if apply:
        con.executemany(
            "UPDATE text_entries SET madoz_entry_id=? WHERE id=?",
            [(p["mid"], p["tid"]) for p in proposed],
        )
    return proposed


def phase2_relink_wrong_sense(
    con: duckdb.DuckDBPyConnection, *, apply: bool
) -> list[dict]:
    """For text_entries already linked, swap to a better-aligned candidate
    when (a) current link has place_type mismatch (score 0), (b) a strictly
    better candidate exists for the same bare lemma, (c) target is unambiguous.

    Never sets a link to NULL.
    """
    text_rows = con.execute("""
        SELECT t.id, t.title, t.place_type, t.madoz_entry_id,
               m.title, m.place_type, m.content_length
        FROM text_entries t
        JOIN madoz_entries m ON t.madoz_entry_id = m.id
    """).fetchall()
    madoz_rows = con.execute("""
        SELECT id, title, place_type, content_length FROM madoz_entries
    """).fetchall()

    by_lemma: dict[str, list[tuple]] = {}
    for mid, mt, mpt, mlen in madoz_rows:
        cl = core_lemma(mt)
        if cl:
            by_lemma.setdefault(cl, []).append((mid, mt, mpt, mlen))

    proposed = []
    for tid, ttitle, tpt, cur_mid, cur_mt, cur_mpt, cur_mlen in text_rows:
        cur_score = pt_aligned(tpt, cur_mpt)
        # Two distinct cases warrant a relink:
        # (a) place_type mismatch (cur_score <= 1) AND a better-aligned
        #     candidate exists (the original Phase 2 logic).
        # (b) current link is to a *stub* curated entry (< 200 chars)
        #     while a substantive same-lemma + same-pt candidate exists
        #     (ALAYOR linked to 54-char stub instead of 5555-char full).
        cl = core_lemma(ttitle)
        candidates = list(by_lemma.get(cl, []))
        alias = LEMMA_ALIAS.get(cl)
        if alias:
            candidates.extend(by_lemma.get(alias, []))
        # Score by (pt_alignment, content_length) descending
        scored = sorted(
            (
                (pt_aligned(tpt, mpt), mlen or 0, mid, mt, mpt)
                for mid, mt, mpt, mlen in candidates
            ),
            reverse=True,
        )
        if not scored:
            continue
        top_score, top_len, top_mid, top_mt, top_mpt = scored[0]
        # Case (a): clear place_type improvement
        better_pt = top_score > cur_score
        # Case (b): same pt (>= 1, so we have *some* alignment) and the
        # current link is a stub. Score must be >= 1 to avoid relinking
        # to a different-place homonym when neither side carries pt info
        # (ATALAYA torre Mallorca was being relinked to ATALAYA monte
        # Ibiza because cur_score=0 == top_score=0).
        better_stub = (
            top_score >= cur_score
            and top_score >= 1
            and (cur_mlen or 0) < 200
            and top_len >= 200
            and top_mid != cur_mid
        )
        if not (better_pt or better_stub):
            continue
        top_band = [s for s in scored if s[0] == top_score and s[1] == top_len]
        if len(top_band) != 1:
            continue
        if top_mid == cur_mid:
            continue
        proposed.append({
            "phase": 2, "tid": tid, "ttitle": ttitle, "tpt": tpt,
            "cur_mid": cur_mid, "cur_mpt": cur_mpt, "cur_mlen": cur_mlen,
            "new_mid": top_mid, "new_mt": top_mt, "new_mpt": top_mpt,
            "new_mlen": top_len,
            "cur_score": cur_score, "new_score": top_score,
            "reason": "stub→substantive" if better_stub and not better_pt else "pt improvement",
        })

    if apply:
        con.executemany(
            "UPDATE text_entries SET madoz_entry_id=? WHERE id=?",
            [(p["new_mid"], p["tid"]) for p in proposed],
        )
    return proposed


_DIGIT_FIX = str.maketrans({"1": "I", "0": "O", "5": "S", "8": "B", "4": "A"})


def compressed_lemma(title: str) -> str:
    """Aggressively normalize a title for cross-source title comparison.

    Strips all whitespace, accents, punctuation, and applies OCR-digit
    substitutions (4 ≈ A, 1 ≈ I, 5 ≈ S, etc.). This makes 'AKL4NT' and
    'ARIANT' compare as a near-match, and 'ALC ARIAS (las)' vs
    'ALCARIAS (las)' as identical. Lossy on purpose — only for dedup.
    """
    if not title:
        return ""
    s = title.upper()
    s = _strip_accents(s)
    s = re.sub(r"[^A-Z0-9]", "", s)
    s = s.translate(_DIGIT_FIX)
    return s


def title_covered_on_leaf(
    chocr_title: str,
    leaf_text_titles: list[str],
    *,
    strict: bool = False,
) -> bool:
    """True if any text_entry title on the same leaf already represents
    the chocr article. Robust against OCR variants and curatorial
    expansions ('AGUILAS' vs 'AGUILAS (punta de las)').

    strict=True drops the fuzzy-ratio path; only exact/substring matches
    count. Use for global (cross-leaf) comparisons where 80% fuzz hits
    catch unrelated short words ('VETA' vs 'VILETA').
    """
    ck = compressed_lemma(chocr_title)
    if not ck:
        return False
    for t in leaf_text_titles:
        tk = compressed_lemma(t)
        if not tk:
            continue
        if ck == tk or ck in tk or tk in ck:
            return True
        if not strict:
            from rapidfuzz import fuzz
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
    the given lemma.

    Madoz repeats the same lemma across regions ('SANTA EUGENIA' in
    Girona, again in Mallorca) on a single leaf, alphabetised by
    sub-province. We collect every paragraph starting with the lemma,
    prefer those containing a Balearic token, and return the longest.

    Strips leaf-boundary markers and 3-char running headers ("824 PET")
    from the window text first, so a paragraph that crosses a leaf
    boundary in Madoz appears as one continuous paragraph here.
    """
    # Drop staging-script markers, leaf-boundary markers, and OCR running
    # headers ("NNN XXX" at line start — a printed page number followed
    # by 3-letter section header).
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


def phase3_promote_chocr(
    con: duckdb.DuckDBPyConnection, *, apply: bool
) -> list[dict]:
    """For each madoz_entry without a text_entry pointing to it, look for
    a matching chocr_entry and promote it into text_entries.

    Madoz-centric (one madoz → at most one new text_entry) so chocr's
    overlapping-window duplicates (LLOBERA listed on leaves 507 *and*
    509) don't multiply rows.
    """
    # Substantive curated Balearic entries with no text_entry yet
    unlinked_madoz = con.execute("""
        SELECT id, title, place_type, island, municipality,
               content_length, content_text
        FROM madoz_entries m
        WHERE NOT EXISTS (
            SELECT 1 FROM text_entries t WHERE t.madoz_entry_id = m.id
        )
          AND content_length >= 200
    """).fetchall()

    # Index chocr by core_lemma → list of candidate rows.
    chocr_rows = con.execute("""
        SELECT c.id, c.vol, c.leaf, c.page_printed, c.title, c.context
        FROM chocr_entries c
        WHERE c.source = 'regex'
          AND c.madoz_entry_id IS NULL
    """).fetchall()
    chocr_by_lemma: dict[str, list[tuple]] = {}
    for c in chocr_rows:
        cl = core_lemma(c[4])
        if cl:
            chocr_by_lemma.setdefault(cl, []).append(c)

    # Existing (vol, leaf, lemma) to avoid inserting a duplicate row for
    # an article already present (under a slightly different title).
    existing = con.execute("SELECT vol, leaf, title FROM text_entries").fetchall()
    existing_idx: dict[tuple, set[str]] = {}
    for vol, leaf, t in existing:
        existing_idx.setdefault((vol, leaf), set()).add(core_lemma(t))

    next_id = (con.execute("SELECT max(id) FROM text_entries").fetchone()[0] or 0) + 1

    proposed = []
    for mid, mt, mpt, misl, mmuni, mlen, mtext in unlinked_madoz:
        cl = core_lemma(mt)
        if not cl or len(cl) < 4:
            continue
        candidates = list(chocr_by_lemma.get(cl, []))
        candidates += [
            r for a in [LEMMA_ALIAS.get(cl)] if a for r in chocr_by_lemma.get(a, [])
        ]
        if not candidates:
            continue

        # Pick the chocr candidate whose inferred place_type aligns with
        # the curated madoz_entry's place_type. If many align, take the
        # first (chocr's overlapping windows duplicate the same article
        # — any of them yields the same paragraph).
        scored = []
        for cid, vol, leaf, pp, ctitle, ctx in candidates:
            chocr_pt = infer_place_type(ctx)
            score = pt_aligned(chocr_pt, mpt) if chocr_pt and mpt else 0
            scored.append((score, cid, vol, leaf, pp, ctitle, ctx))
        scored.sort(reverse=True)

        # Try candidates in score order
        chosen = None
        for score, cid, vol, leaf, pp, ctitle, ctx in scored:
            # Skip if a text_entry on the same (vol, leaf) already has this lemma
            if cl in existing_idx.get((vol, leaf), set()):
                continue
            window = _load_chocr_window(vol, leaf)
            if window is None:
                continue
            para = _extract_paragraph(
                window, ctitle.split("(")[0].strip()
            )
            if para is None or len(para) < 120:
                continue
            if not is_balearic_text(para):
                continue
            chosen = (cid, vol, leaf, pp, ctitle, para, score)
            break

        if not chosen:
            continue
        cid, vol, leaf, pp, ctitle, para, score = chosen
        proposed.append({
            "phase": 3, "new_tid": next_id, "vol": vol, "leaf": leaf,
            "page_printed": pp, "title": ctitle, "place_type": mpt,
            "island": misl, "municipality": mmuni,
            "description": para[:8000],
            "chocr_entry_id": cid, "madoz_entry_id": mid,
            "confidence": "unverified",
            "note": "Promoted by rescue_unlinked.py from chocr+curated reconciliation; raw OCR snippet, not Claude-extracted",
            "source_file": f"data/text/_chocr/page_{vol}_{leaf}.txt",
            "_pt_score": score,
        })
        existing_idx.setdefault((vol, leaf), set()).add(cl)
        next_id += 1

    if apply and proposed:
        for p in proposed:
            con.execute(
                """INSERT INTO text_entries
                   (id, vol, leaf, page_printed, title, place_type, island,
                    municipality, description, confidence, model, source_file,
                    note, chocr_entry_id, madoz_entry_id, extracted_at,
                    description_raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), ?)""",
                [
                    p["new_tid"], p["vol"], p["leaf"], p["page_printed"],
                    p["title"], p["place_type"], p["island"], p["municipality"],
                    p["description"], p["confidence"], "chocr-snippet",
                    p["source_file"], p["note"], p["chocr_entry_id"],
                    p["madoz_entry_id"], p["description"],
                ],
            )
    return proposed


# Peninsular toponym lemmas — articles bearing these names CANNOT be
# Balearic, even if the chocr Balearic filter caught a passing mention
# (Valencia archbishop article mentions Mallorca and Menorca as suffragans).
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
    """True if the bare lemma (sans parens) is a peninsular toponym."""
    if not title:
        return False
    bare = re.sub(r"\([^)]*\)", "", title).strip()
    bare = re.sub(r"\s+", " ", bare).upper()
    bare = _strip_accents(bare)
    return bare in PENINSULAR_LEMMA_BLOCK


def _looks_like_real_lemma(title: str) -> bool:
    """Reject chocr titles that are obvious junk (mid-sentence
    fragments captured by the regex). A real Madoz lemma is a short
    all-caps string optionally followed by a parenthesised qualifier.
    """
    if not title:
        return False
    if len(title) > 50:
        return False
    # Strip parens content; the rest must be all-uppercase (with OCR
    # digits/punctuation) and contain no lowercase letters.
    bare = re.sub(r"\([^)]*\)", "", title).strip()
    bare = re.sub(r"\s+", " ", bare)
    if not bare:
        return False
    # A real lemma may have OCR digits, accents, hyphens; reject if it
    # has any lowercase letter (e.g. 'TERRENO es de buena calidad...').
    if re.search(r"\b[a-z]+\b", bare):
        return False
    return True


def phase4_promote_chocr_orphans(
    con: duckdb.DuckDBPyConnection, *, apply: bool
) -> list[dict]:
    """Promote chocr_entries that have NO text_entry covering them on the
    same leaf (by aggressive title comparison).

    Unlike Phase 3, this doesn't require a curated mirror match. The
    rationale: if the chocr regex indexed an article with a Balearic
    context AND no text_entry on that leaf represents it, we have a
    bona-fide miss in our corpus. Insert with madoz_entry_id from a
    fuzzy curated match if available, else NULL.
    """
    chocr_rows = con.execute("""
        SELECT id, vol, leaf, page_printed, title, context
        FROM chocr_entries
        WHERE source = 'regex' AND madoz_entry_id IS NULL
    """).fetchall()

    # text_entries by (vol, leaf) → list of (id, title) AND
    # by vol → list of all titles (to catch articles indexed under a
    # different leaf via chocr-window overlap)
    text_by_leaf: dict[tuple, list[tuple]] = {}
    text_by_vol: dict[str, list[str]] = {}
    for r in con.execute(
        "SELECT id, vol, leaf, title FROM text_entries"
    ).fetchall():
        text_by_leaf.setdefault((r[1], r[2]), []).append((r[0], r[3]))
        text_by_vol.setdefault(r[1], []).append(r[3])

    # madoz_entries by core_lemma (for opportunistic curated link)
    madoz_by_lemma: dict[str, list[tuple]] = {}
    for r in con.execute("""
        SELECT id, title, place_type, island, municipality, content_length
        FROM madoz_entries
    """).fetchall():
        cl = core_lemma(r[1])
        if cl:
            madoz_by_lemma.setdefault(cl, []).append(r)

    next_id = (con.execute("SELECT max(id) FROM text_entries").fetchone()[0] or 0) + 1

    proposed = []
    seen_madoz: set[int] = set()
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
        # Cross-leaf dedup: chocr's overlapping windows index the same
        # article on consecutive leaves (ADAYA on leaves 84 + 85). If the
        # title is already covered anywhere in the same volume by a
        # near-exact match, skip.
        ck = compressed_lemma(ctitle)
        vol_titles = text_by_vol.get(vol, [])
        if ck and any(
            compressed_lemma(t) == ck for t in vol_titles
        ):
            continue
        # Genuine miss — find article paragraph in chocr window
        window = _load_chocr_window(vol, leaf)
        if window is None:
            # Fall back to the chocr_entries.context field directly. It's
            # only ~200 chars but at least it gives us *something*.
            para = ctx
        else:
            lemma_for_lookup = ctitle.split("(")[0].strip()
            para = _extract_paragraph(window, lemma_for_lookup)
            if para is None:
                para = ctx  # fallback
        if not para or len(para) < 60:
            continue
        if not is_balearic_text(para):
            continue

        # Opportunistic curated link
        chocr_pt = infer_place_type(ctx)
        cl = core_lemma(ctitle)
        mid = None
        mpt = misl = mmuni = None
        cands = madoz_by_lemma.get(cl, [])
        cands += [r for a in [LEMMA_ALIAS.get(cl)] if a for r in madoz_by_lemma.get(a, [])]
        if cands:
            if chocr_pt:
                aligned = [c for c in cands if pt_aligned(chocr_pt, c[2]) >= 2]
                if len(aligned) == 1:
                    mid, _, mpt, misl, mmuni, _ = aligned[0]
                elif len(aligned) > 1:
                    best = max(aligned, key=lambda r: r[5] or 0)
                    mid, _, mpt, misl, mmuni, _ = best
            elif len(cands) == 1:
                mid, _, mpt, misl, mmuni, _ = cands[0]
        if mid in seen_madoz and mid is not None:
            # Already promoted another chocr for this madoz_entry in this run
            mid = None

        # Try to derive place_type from the para opener if curated didn't tell us
        if not mpt:
            mpt = infer_place_type(para)

        proposed.append({
            "phase": 4, "new_tid": next_id, "vol": vol, "leaf": leaf,
            "page_printed": pp, "title": ctitle, "place_type": mpt,
            "island": misl, "municipality": mmuni,
            "description": para[:8000],
            "chocr_entry_id": cid, "madoz_entry_id": mid,
            "confidence": "unverified",
            "note": "Promoted by rescue_unlinked.py Phase 4 (chocr orphan, no same-leaf text_entry)",
            "source_file": (
                f"data/text/_chocr/page_{vol}_{leaf}.txt"
                if window else "chocr_entries.context"
            ),
        })
        text_by_leaf.setdefault((vol, leaf), []).append((next_id, ctitle))
        text_by_vol.setdefault(vol, []).append(ctitle)
        if mid is not None:
            seen_madoz.add(mid)
        next_id += 1

    if apply and proposed:
        for p in proposed:
            con.execute(
                """INSERT INTO text_entries
                   (id, vol, leaf, page_printed, title, place_type, island,
                    municipality, description, confidence, model, source_file,
                    note, chocr_entry_id, madoz_entry_id, extracted_at,
                    description_raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), ?)""",
                [
                    p["new_tid"], p["vol"], p["leaf"], p["page_printed"],
                    p["title"], p["place_type"], p["island"], p["municipality"],
                    p["description"], p["confidence"], "chocr-snippet",
                    p["source_file"], p["note"], p["chocr_entry_id"],
                    p["madoz_entry_id"], p["description"],
                ],
            )
    return proposed


# Curated-mirror entries whose title is an OCR typo of an article we
# already have under the canonical spelling. Promoting them creates a
# duplicate; resolution is a Phase-2-style relink which Phase 5 can't
# perform without losing safety.
CURATED_OCR_DUPS = {
    21285,  # 'CALOBKA (La)' is OCR for CALOBRA (la); we already have
            # text_entry 8075 CALOBRA — needs a manual relink, not a new row.
}

# Letter → most-likely Madoz volume. Used to guess `vol` for Phase 5
# curated-mirror promotions, since the curated mirror itself doesn't
# track volume. Coarse but workable for navigation: a reader can locate
# the article on the IA facsimile of that volume. `leaf` is set to 0 as
# a sentinel signalling "no chocr leaf known".
LETTER_TO_VOL = {
    "A": "01", "B": "04", "C": "06", "D": "07", "E": "07", "F": "08",
    "G": "08", "H": "09", "I": "09", "J": "09", "K": "09", "L": "10",
    "M": "11", "N": "12", "O": "12", "P": "12", "Q": "13", "R": "13",
    "S": "13", "T": "15", "U": "16", "V": "16", "W": "16", "X": "16",
    "Y": "16", "Z": "16",
}


def _guess_vol_from_lemma(title: str) -> str:
    """Best-guess Madoz volume by the first letter of the lemma (post-
    stripping LA/SAN/SO articles). Returns the placeholder '00' if the
    lemma is empty or has no recognisable first letter."""
    cl = core_lemma(title)
    if not cl:
        return "00"
    first = cl[0].upper()
    return LETTER_TO_VOL.get(first, "00")


def phase5_promote_curated_only(
    con: duckdb.DuckDBPyConnection, *, apply: bool
) -> list[dict]:
    """Promote substantive curated Balearic entries that have *no chocr
    counterpart and no text_entry* — the cases where indent-detection
    upstream would have helped but the chocr regex didn't catch them.

    For these we trust the curated mirror as the source-of-truth body
    text. confidence='unverified', model='curated-mirror'. (vol, leaf,
    page_printed) come from the curated entry if known; otherwise NULL.
    """
    unlinked = con.execute("""
        SELECT m.id, m.title, m.place_type, m.island, m.municipality,
               m.content_length, m.content_text
        FROM madoz_entries m
        WHERE NOT EXISTS (SELECT 1 FROM text_entries t WHERE t.madoz_entry_id = m.id)
          AND content_length >= 200
          AND m.island IS NOT NULL
    """).fetchall()

    # All text_entries titles (we don't know which vol the curated
    # entry belongs to, so check globally).
    all_text_titles = [
        r[0] for r in con.execute("SELECT title FROM text_entries").fetchall()
    ]

    next_id = (con.execute("SELECT max(id) FROM text_entries").fetchone()[0] or 0) + 1

    proposed = []
    for mid, mt, mpt, misl, mmuni, mlen, mtext in unlinked:
        if mid in CURATED_OCR_DUPS:
            continue
        ck = compressed_lemma(mt)
        if not ck:
            continue
        # Skip if any text_entry's title is a substring/superstring match
        # (catches 'BLANCO' curated vs 'BLANCO (cabo, Mallorca)' text).
        if title_covered_on_leaf(mt, all_text_titles, strict=True):
            continue
        # Verify the content is unambiguously Balearic
        if not is_balearic_text(mtext or ""):
            continue
        proposed.append({
            "phase": 5, "new_tid": next_id,
            "vol": _guess_vol_from_lemma(mt), "leaf": 0,
            "page_printed": None, "title": mt, "place_type": mpt,
            "island": misl, "municipality": mmuni,
            "description": (mtext or "")[:8000],
            "chocr_entry_id": None, "madoz_entry_id": mid,
            "confidence": "unverified",
            "note": "Promoted by rescue_unlinked.py Phase 5 from curated mirror; no chocr counterpart found",
            "source_file": "data/madoz/posts.jsonl",
        })
        all_text_titles.append(mt)
        next_id += 1

    if apply and proposed:
        for p in proposed:
            con.execute(
                """INSERT INTO text_entries
                   (id, vol, leaf, page_printed, title, place_type, island,
                    municipality, description, confidence, model, source_file,
                    note, chocr_entry_id, madoz_entry_id, extracted_at,
                    description_raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), ?)""",
                [
                    p["new_tid"], p["vol"], p["leaf"], p["page_printed"],
                    p["title"], p["place_type"], p["island"], p["municipality"],
                    p["description"], p["confidence"], "curated-mirror",
                    p["source_file"], p["note"], p["chocr_entry_id"],
                    p["madoz_entry_id"], p["description"],
                ],
            )
    return proposed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Commit changes. Default is dry-run.")
    ap.add_argument("--phase", type=int, choices=[1, 2, 3, 4, 5],
                    help="Run only one phase.")
    args = ap.parse_args()

    if not DB.exists():
        sys.exit(f"DB not found: {DB}")

    con = duckdb.connect(str(DB), read_only=not args.apply)

    before_n = con.execute("SELECT count(*) FROM text_entries").fetchone()[0]
    before_linked = con.execute(
        "SELECT count(*) FROM text_entries WHERE madoz_entry_id IS NOT NULL"
    ).fetchone()[0]

    if args.phase in (None, 1):
        ph1 = phase1_fill_nulls(con, apply=args.apply)
        print(f"\n=== Phase 1: fill NULL madoz_entry_id ({len(ph1)} proposals) ===")
        for p in ph1[:40]:
            print(f"  text {p['tid']} \"{p['ttitle'][:35]:35s}\" pt={p['tpt'] or '—':18s}"
                  f" → madoz {p['mid']} \"{p['mt'][:25]:25s}\" pt={p['mpt'] or '—':18s} (pt-score {p['score']})")
        if len(ph1) > 40:
            print(f"  ... and {len(ph1) - 40} more")

    if args.phase in (None, 2):
        ph2 = phase2_relink_wrong_sense(con, apply=args.apply)
        print(f"\n=== Phase 2: relink wrong-sense FKs ({len(ph2)} proposals) ===")
        for p in ph2[:40]:
            print(f"  text {p['tid']} \"{p['ttitle'][:35]:35s}\" pt={p['tpt'] or '—':18s}"
                  f" : madoz {p['cur_mid']}→{p['new_mid']} (pt {p['cur_mpt'] or '—'} → {p['new_mpt'] or '—'},"
                  f" score {p['cur_score']}→{p['new_score']}, {p.get('reason', '?')})")
        if len(ph2) > 40:
            print(f"  ... and {len(ph2) - 40} more")

    if args.phase in (None, 3):
        ph3 = phase3_promote_chocr(con, apply=args.apply)
        print(f"\n=== Phase 3: promote chocr-only Balearic articles ({len(ph3)} proposals) ===")
        for p in ph3[:40]:
            print(f"  new text {p['new_tid']}: \"{p['title'][:35]:35s}\""
                  f" vol={p['vol']} leaf={p['leaf']} → madoz {p['madoz_entry_id']}"
                  f" desc-len={len(p['description'])}")
        if len(ph3) > 40:
            print(f"  ... and {len(ph3) - 40} more")

    if args.phase in (None, 4):
        ph4 = phase4_promote_chocr_orphans(con, apply=args.apply)
        print(f"\n=== Phase 4: promote chocr orphans (no same-leaf coverage) ({len(ph4)} proposals) ===")
        for p in ph4[:60]:
            mid_label = str(p["madoz_entry_id"]) if p["madoz_entry_id"] else "—"
            print(f"  new text {p['new_tid']}: \"{p['title'][:35]:35s}\""
                  f" vol={p['vol']} leaf={p['leaf']} pt={p['place_type'] or '—':12s}"
                  f" → madoz {mid_label} desc-len={len(p['description'])}")
        if len(ph4) > 60:
            print(f"  ... and {len(ph4) - 60} more")

    if args.phase in (None, 5):
        ph5 = phase5_promote_curated_only(con, apply=args.apply)
        print(f"\n=== Phase 5: promote curated-only Balearic entries ({len(ph5)} proposals) ===")
        for p in ph5[:40]:
            print(f"  new text {p['new_tid']}: \"{p['title'][:35]:35s}\""
                  f" isl={p['island'] or '—':10s} pt={p['place_type'] or '—':14s}"
                  f" → madoz {p['madoz_entry_id']} (curated, no chocr)")

    after_n = con.execute("SELECT count(*) FROM text_entries").fetchone()[0]
    after_linked = con.execute(
        "SELECT count(*) FROM text_entries WHERE madoz_entry_id IS NOT NULL"
    ).fetchone()[0]
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"\n=== Summary [{mode}] ===")
    print(f"  text_entries:    {before_n} → {after_n}")
    print(f"  with madoz link: {before_linked} → {after_linked}")
    print(f"  net coverage:    {after_n - before_n:+d} entries,"
          f" {after_linked - before_linked:+d} links")


if __name__ == "__main__":
    main()
