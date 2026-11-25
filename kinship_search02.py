#!/usr/bin/env python3
"""KINSHIP-SEARCH-02 — targeted Иван/Пётр family search over the OCR index.

Scope: all four archival cases (253, 255, 256, 258 — ЦГА Москвы ф. 597 оп. 1,
исповедные ведомости села Протасьева).  No corpus OCR rerun; this works on
the existing index (SQLite + FTS5 + trigram, 56 738 lines).

Pipeline (reuses the Stage D stack: htr_search.normalize, profiles.corrupt,
Searcher FTS/trigram recall, RapidFuzz fuzzy layer, line bboxes):
  1. recall pool: SQL-prefix scan (guarantees complete exact-stem counts) ∪
     Searcher._fts_candidates ∪ Searcher._trigram_candidates(corruption
     variants) — cross-checked against a full-corpus token pass;
  2. token classification: given-name forms distinguished from patronymic
     forms (Иван Петровъ ≠ Пётр Ивановъ); tiers exact / glued / fuzzy, raw
     OCR forms preserved and flagged, never silently corrected;
  3. column-aware context (each scan = two register pages; split at
     page mid-x, order by y) and household segmentation from numbering +
     relation markers («жена», «сын», «дети», «брат», «вдова», historical
     and OCR-garbled forms); ±8-line context, overlapping windows deduped
     into household groups;
  4. pattern classification P1–P5 (see PATTERNS below);
  5. ranking: documented relationship > co-residence > absence; exact
     personal-name matches above fuzzy; confirmed Волченокъ sections ranked
     above other settlements; landowner surnames never count as family
     evidence; recurrence across years is a flagged lead, never an identity
     merge.

Outputs (work/discovery/kinship-search02/):
  kinship-search02.json        — full export (totals, groups, ranking)
  crops/<group>-*.png          — source crops for top groups
  kinship-search02-review.html  — compact human-review page

All name readings below are UNVERIFIED OCR leads; nothing is a verified
person and no identity is merged automatically.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, "/Users/mysterx/dev/kinship-htr-bench")

from rapidfuzz import fuzz  # noqa: E402

from htr_search.normalize import normalize  # noqa: E402
from htr_search.profiles import corrupt  # noqa: E402
from htr_search.search import Searcher  # noqa: E402

BENCH = Path("/Users/mysterx/dev/kinship-htr-bench")
DB = BENCH / "work" / "index" / "htr.db"
OUT_DIR = BENCH / "work" / "discovery" / "kinship-search02"
CROP_DIR = OUT_DIR / "crops"
TOP_N_REPORT = 30
CONTEXT_K = 8  # ± lines of context around a household

# ---------------------------------------------------------------- names ---
# Given-name stems (normalized: no ъ/ь, ё→е, і→и) vs patronymic stems.
IVAN_GIVEN = ("иван", "иоанн")
IVAN_PATR = ("иванов", "иоаннов")
PETR_GIVEN = ("петр",)
PETR_PATR = ("петров",)
GIVEN_INFL = {"", "а", "у", "ом", "е", "ы", "и"}  # nominative + seen obliques
# 1-char glue prefixes seen in OCR (у Петр, его Иван glued into one token)
GLUE_PREFIX_OK = {"его", "их", "ея", "ее", "у", "в", "с", "на", "за", "по",
                  "от", "се", "вдов", "вдова", "при"}
STATUS_TOKENS = {"вдов", "вдова", "холост", "холостъ"}

# fuzzy thresholds (token-level, after digit stripping)
FZ_EXACT = 74     # ratio vs stem for 4-char stems
FZ_LONG = 80      # ratio for longer tokens
FZ_PARTIAL = 82   # partial_ratio fallback for 5-8 char tokens
FZ_PATR = 82      # ratio vs patronymic stem

NUM_RE = re.compile(r"^[\d\s.,:;]+$")
YEAR_RE = re.compile(r"1(8[0-4][0-9])")

# ------------------------------------------------------------ relations ---
# relation stems (normalized); matched with fuzz.ratio >= 74 or prefix-in
REL_STEMS = {
    "жена": "spouse", "жену": "spouse", "женя": "spouse",
    "сын": "son",
    "дочь": "daughter", "дщерь": "daughter",
    "дети": "children", "дти": "children", "детеи": "children",
    "брат": "brother", "брата": "brother", "братья": "brother",
    "сестра": "sister", "сестру": "sister",
    "вдова": "widow", "вдов": "widower-mark",
    "приемыш": "foster", "приемур": "foster",
    "отец": "father", "мать": "mother",
    "внуки": "grandchildren", "племянник": "nephew",
}
REL_TARGETS = {"его", "их", "из", "ея", "ее", "е", "ихъ"}
FZ_REL = 74

HEADER_WORDS = {"число", "лета", "лето", "рождения", "людей", "кто", "были",
                "исповедь", "исповеда", "исповедались", "исповедались",
                "показание", "показан", "действа", "действия", "святаго",
                "причастились", "прича", "неисповед", "ведомост", "ведомости",
                "округи", "епархии", "московской", "церкви", "села"}

# ------------------------------------------------- verified Волченокъ ---
# From work/discovery/volchenok-sections-verified.json (Phase B, AI-assisted
# visual verification, disclosed as NOT paleographic certification).
# spans: (page_id, side, y_lo|None, y_hi|None)
SECTIONS = [
    {"case": "253", "label": "253-A", "cls": "probable",
     "spans": [("253-p080", "L", 349, None), ("253-p081", "L", None, None),
               ("253-p081", "R", None, None), ("253-p082", "L", None, None),
               ("253-p082", "R", None, None), ("253-p083", "L", None, None)]},
    {"case": "255", "label": "255-A", "cls": "probable",
     "spans": [("255-p026", "L", 1314, None), ("255-p026", "R", None, None),
               ("255-p027", "L", None, None), ("255-p027", "R", None, None),
               ("255-p028", "L", None, None), ("255-p028", "R", None, None),
               ("255-p029", "L", None, None), ("255-p029", "R", None, None),
               ("255-p030", "L", None, None), ("255-p030", "R", None, 1488)]},
    {"case": "255", "label": "255-B", "cls": "probable",
     "spans": [("255-p042", "L", 1704, None), ("255-p042", "R", None, None),
               ("255-p043", "L", None, None), ("255-p043", "R", None, None),
               ("255-p044", "L", None, None), ("255-p044", "R", None, None),
               ("255-p045", "L", None, None), ("255-p045", "R", None, None),
               ("255-p046", "L", None, None), ("255-p046", "R", None, 1456)]},
    {"case": "255", "label": "255-C", "cls": "probable",
     "spans": [("255-p077", "L", 514, None), ("255-p077", "R", None, None),
               ("255-p078", "L", None, None), ("255-p078", "R", None, None),
               ("255-p079", "L", None, None), ("255-p079", "R", None, None),
               ("255-p080", "L", None, None), ("255-p080", "R", None, None),
               ("255-p081", "L", None, None), ("255-p081", "R", None, None),
               ("255-p082", "L", None, None), ("255-p082", "R", None, None)]},
    {"case": "256", "label": "256-A", "cls": "probable",
     "spans": [("256-p016", "L", 511, None), ("256-p016", "R", None, None),
               ("256-p017", "L", None, None), ("256-p017", "R", None, None),
               ("256-p018", "L", None, None), ("256-p018", "R", None, None),
               ("256-p019", "L", None, None)]},
    {"case": "256", "label": "256-B", "cls": "probable",
     "spans": [("256-p053", "L", 1576, None), ("256-p053", "R", None, None),
               ("256-p054", "L", None, None), ("256-p054", "R", None, None),
               ("256-p055", "L", None, None), ("256-p055", "R", None, None),
               ("256-p056", "L", None, None), ("256-p056", "R", None, None),
               ("256-p057", "L", None, None), ("256-p057", "R", None, None),
               ("256-p058", "L", None, None)]},
    {"case": "258", "label": "258-A", "cls": "confirmed",
     "spans": [("258-p014", "L", 1322, None), ("258-p014", "R", None, None),
               ("258-p015", "L", None, None), ("258-p015", "R", None, None),
               ("258-p016", "L", None, None), ("258-p016", "R", None, None),
               ("258-p017", "L", None, None), ("258-p017", "R", None, None),
               ("258-p018", "L", None, None), ("258-p018", "R", None, None),
               ("258-p019", "L", None, None), ("258-p019", "R", None, None),
               ("258-p020", "L", None, None)]},
    {"case": "258", "label": "258-B", "cls": "probable",
     "spans": [("258-p032", "L", 1882, None), ("258-p032", "R", None, None),
               ("258-p033", "L", None, None), ("258-p033", "R", None, None),
               ("258-p034", "L", None, None), ("258-p034", "R", None, None),
               ("258-p035", "L", None, None), ("258-p035", "R", None, None),
               ("258-p036", "L", None, None), ("258-p036", "R", None, None),
               ("258-p037", "L", None, None), ("258-p037", "R", None, None),
               ("258-p038", "L", None, None), ("258-p038", "R", None, None),
               ("258-p039", "L", None, None), ("258-p039", "R", None, None),
               ("258-p040", "L", None, None), ("258-p040", "R", None, None),
               ("258-p041", "L", None, None), ("258-p041", "R", None, None),
               ("258-p042", "L", None, None), ("258-p042", "R", None, None),
               ("258-p043", "L", None, None), ("258-p043", "R", None, 1004)]},
    {"case": "258", "label": "258-C", "cls": "confirmed",
     "spans": [("258-p068", "R", 623, None), ("258-p069", "L", None, None),
               ("258-p069", "R", None, None), ("258-p070", "L", None, None),
               ("258-p070", "R", None, None), ("258-p071", "L", None, None),
               ("258-p071", "R", None, None), ("258-p072", "L", None, None),
               ("258-p072", "R", None, None), ("258-p073", "L", None, None),
               ("258-p073", "R", None, None)]},
]
SECTION_SPANS = {}
for _s in SECTIONS:
    for _pid, _side, _ylo, _yhi in _s["spans"]:
        SECTION_SPANS[(_pid, _side)] = (_s, _ylo, _yhi)

CASE_RANGE = {"253": (1813, 1819), "255": (1820, 1824),
              "256": (1825, 1829), "258": (1830, 1834)}
ARCHIVE = ("ЦГА Москвы, ф. 597, оп. 1 (Верейское духовное правление, "
           "исповедные ведомости села Протасьева)")


# ------------------------------------------------------- token matching ---
def strip_digits(tok: str) -> str:
    return tok.strip("0123456789")


def match_name_token(t: str) -> dict | None:
    """Classify one normalized, digit-stripped token as an Ivan/Pyotr form.

    Returns {"name","form","tier","stem","raw"} or None.
    form: given | patronymic ; tier: exact | glued | fuzzy
    """
    if not t or len(t) < 3:
        return None
    for name, given_stems, patr_stems in (
            ("иван", IVAN_GIVEN, IVAN_PATR),
            ("петр", PETR_GIVEN, PETR_PATR)):
        for stem in patr_stems:
            if t.startswith(stem):
                return {"name": name, "form": "patronymic", "tier": "exact",
                        "strong": True, "stem": stem, "raw": t}
        # patronymic glued inside a longer token («праскеваиванова» =
        # Праскева Иванова fused by the OCR line merge)
        for stem in patr_stems:
            i = t.find(stem, 1)
            if 0 < i and (len(t) - i - len(stem)) <= 2:
                return {"name": name, "form": "patronymic", "tier": "glued",
                        "strong": True, "stem": stem, "raw": t}
        for stem in given_stems:
            if t == stem:
                return {"name": name, "form": "given", "tier": "exact",
                        "strong": True, "stem": stem, "raw": t}
            if t.startswith(stem):
                rest = t[len(stem):]
                if rest in GIVEN_INFL:
                    return {"name": name, "form": "given", "tier": "exact",
                            "strong": True, "stem": stem, "raw": t}
                # glued prefix + stem (+ inflection)
                for plen in (4, 3, 2, 1):
                    if len(t) > plen and t[:plen] in GLUE_PREFIX_OK \
                            and t[plen:].startswith(stem):
                        rest = t[plen + len(stem):]
                        if rest in GIVEN_INFL:
                            return {"name": name, "form": "given",
                                    "tier": "glued", "strong": True,
                                    "stem": stem, "raw": t}
                # stem glued INSIDE the token (prefix unknown-garble)
                i = t.find(stem, 1)
                if i > 0 and t[i + len(stem):] in GIVEN_INFL:
                    return {"name": name, "form": "given", "tier": "glued",
                            "strong": True, "stem": stem, "raw": t}
        best = None
        for stem in given_stems:
            s = fuzz.ratio(t, stem)
            if s >= FZ_EXACT and (best is None or s > best[0]):
                best = (s, stem)
        if best is None and 5 <= len(t) <= 9:
            for stem in given_stems:
                s = fuzz.partial_ratio(stem, t)
                if s >= FZ_PARTIAL and (best is None or s > best[0]):
                    best = (s, stem)
        if best is not None:
            # 75-79 (e.g. «нван», «дина») are weak leads: reported, never
            # pattern-driving; 80+ (e.g. «иоан», «петрр») may claim patterns
            return {"name": name, "form": "given", "tier": "fuzzy",
                    "strong": best[0] >= 80.0, "stem": best[1], "raw": t,
                    "fuzz": round(best[0], 1)}
        for stem in patr_stems:
            if len(t) >= 5 and fuzz.ratio(t, stem) >= FZ_PATR:
                return {"name": name, "form": "patronymic", "tier": "fuzzy",
                        "stem": stem, "raw": t}
    return None


def match_relation_token(t: str) -> tuple[str, str] | None:
    """Leading relation word: (role, raw_token) or None. Fuzzy-tolerant.

    A token that is itself an Ivan/Pyotr name form never reads as a
    relation word.  Fused relation+target garbles («естиихъ» ≈ «дети ихъ»)
    are retried with a trailing их/его stripped before fuzzy matching.
    """
    if not t or len(t) < 3:
        return None
    if match_name_token(t):
        return None
    for cand in (t, t.removesuffix("их"), t.removesuffix("его")):
        if len(cand) < 3:
            continue
        for stem, role in REL_STEMS.items():
            if cand.startswith(stem):
                return role, t
            if fuzz.ratio(cand, stem) >= FZ_REL:
                return role, t
    return None


def is_header_line(tokens: list[str]) -> bool:
    """Printed table-header band («Число / Лѣта отъ рожденія / …»)."""
    alpha = [t for t in tokens if len(t) >= 2]
    if not alpha:
        return False
    hits = 0
    for t in alpha:
        if any(fuzz.ratio(t, h) >= 80 for h in HEADER_WORDS):
            hits += 1
    return hits / len(alpha) >= 0.6


VILLAGE_RE = re.compile(r"деревн|дерьн|дереан|дерев")


def heading_kind(text_norm: str) -> str | None:
    """'village' | 'year' | 'closing' | None for a settlement/year marker."""
    tn = text_norm
    if VILLAGE_RE.search(tn):
        return "village"
    if "ведомост" in tn or "ведомости" in tn:
        return "year"
    if "показаны исповедав" in tn or "показаны испов" in tn:
        return "closing"
    if "вотчин" in tn and "крестьян" not in tn:
        return "village"  # owner+village formula garbles
    return None


def extract_village_name(tokens: list[str]) -> str:
    """OCR tokens after «деревни…» up to the next formula word."""
    out = []
    started = False
    for t in tokens:
        if not started:
            if VILLAGE_RE.search(t):
                rest = VILLAGE_RE.sub("", t)
                started = True
                if rest and len(rest) >= 3:
                    out.append(rest)
            continue
        if t in {"крестьяне", "крестьян", "крестяни", "крестян",
                 "крестьяни", "притвен", "притвень", "и", "их"}:
            break
        if any(fuzz.ratio(t, w) >= 82 for w in
               ("крестьяне", "крестьян", "вотчины", "графини", "графов")):
            break
        out.append(t)
        if len(out) >= 2:
            break
    return " ".join(out)


# ------------------------------------------------------------- DB layer ---
def load_db() -> tuple[sqlite3.Connection, dict]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    pages = {}
    for r in conn.execute("SELECT * FROM pages"):
        pages[r["page_id"]] = dict(r)
    lines = defaultdict(list)
    for r in conn.execute(
            "SELECT line_id, page_id, line_number, source, text_raw, text_norm,"
            " bbox, confidence, reading_order, y_center FROM lines"):
        d = dict(r)
        bbox = json.loads(d["bbox"])
        d["bbox"] = bbox
        d["xc"] = bbox[0] + bbox[2] / 2.0
        d["yc"] = bbox[1] + bbox[3] / 2.0
        lines[d["page_id"]].append(d)
    return conn, {"pages": pages, "lines": lines}


def column_of(page: dict, xc: float) -> str:
    return "L" if xc < (page["width"] or 2850) / 2.0 else "R"


def order_page_columns(page_id: str, page: dict, plines: list[dict]):
    """Lines grouped per register page (column), ordered by (y, x)."""
    cols = {"L": [], "R": []}
    for ln in plines:
        cols[column_of(page, ln["xc"])].append(ln)
    for c in cols.values():
        c.sort(key=lambda l: (l["yc"], l["xc"]))
    return cols


def in_section(case: str, page_id: str, side: str, yc: float):
    ent = SECTION_SPANS.get((page_id, side))
    if ent is None:
        return None
    sec, ylo, yhi = ent
    if sec["case"] != case:
        return None
    if ylo is not None and yc < ylo:
        return None
    if yhi is not None and yc > yhi:
        return None
    return sec


# ---------------------------------------------------------- year layer ---
def year_runs(pages: dict, lines: dict) -> dict:
    """Assign an OCR-derived year per page (unverified; see caveats)."""
    year_events = []   # (case, page_number, yc, year, page_id, kind)
    run_starts = []    # (case, page_number, yc, page_id)
    for pid in sorted(pages, key=lambda p: (pages[p]["case_short"],
                                            pages[p]["page_number"])):
        p = pages[pid]
        for ln in lines.get(pid, []):
            tn = ln["text_norm"]
            if not tn:
                continue
            kind = heading_kind(tn)
            if kind == "year":
                run_starts.append((p["case_short"], p["page_number"],
                                   ln["yc"], pid))
            for m in YEAR_RE.finditer(ln["text_raw"]):
                y = int(m.group(0))
                if 1805 <= y <= 1845:
                    year_events.append((p["case_short"], p["page_number"],
                                        ln["yc"], y, pid, kind or ""))
                break
    # year per page: nearest year event in the same case (|page| distance,
    # tie -> earlier event); runs merely recorded for context
    by_case = defaultdict(list)
    for ev in year_events:
        by_case[ev[0]].append(ev)
    for c in by_case:
        by_case[c].sort(key=lambda e: (e[1], e[2]))
    page_year = {}
    for pid, p in pages.items():
        c = p["case_short"]
        evs = by_case.get(c)
        if not evs:
            page_year[pid] = None
            continue
        best = None
        for ev in evs:
            d = abs(ev[1] - p["page_number"])
            if best is None or d < best[0]:
                best = (d, ev)
        lo, hi = CASE_RANGE[c]
        if best is None:
            page_year[pid] = None
        else:
            y = best[1][3]
            used = lo - 2 <= y <= hi + 2
            page_year[pid] = {
                "value": y if used else None,
                "raw_token": y,
                "evidence_page": f"{best[1][4]}",
                "distance_pages": best[0],
                "method": ("nearest OCR year token (UNVERIFIED reading; "
                           "may be a closing-formula or submission year)"),
                "in_case_range": used,
            }
    return page_year, by_case


# ------------------------------------------------- pool (existing stack) ---
def build_pool(conn, searcher) -> tuple[set[int], dict]:
    """Recall pool: SQL-prefix scan ∪ FTS5 prefix ∪ trigram corruption net."""
    t0 = time.time()
    sql_ids = set()
    for pat in ("иван", "иоан", "петр"):
        for r in conn.execute(
                "SELECT line_id FROM lines WHERE text_norm LIKE ?",
                (f"%{pat}%",)):
            sql_ids.add(r["line_id"])
    fts_ids = searcher._fts_candidates(
        ["иван", "иоанн", "петр", "иванова", "петрова"])
    trgm_ids = set()
    for stem in ("иван", "иоанн", "петр"):
        trgm_ids |= searcher._trigram_candidates(
            [stem] + corrupt(stem, max_variants=80))
    pool = sql_ids | fts_ids | trgm_ids
    stats = {"sql_prefix": len(sql_ids), "fts5_prefix": len(fts_ids),
             "trigram_corruption": len(trgm_ids),
             "union": len(pool), "elapsed_s": round(time.time() - t0, 1)}
    return pool, stats


# ---------------------------------------------------- line tokenization ---
def line_tokens(ln: dict) -> list[dict]:
    """Aligned raw/normalized tokens."""
    toks = []
    for raw_t in ln["text_raw"].split():
        toks.append({"raw": raw_t, "norm": normalize(raw_t)})
    return toks


def classify_line(ln: dict) -> list[dict]:
    """All Ivan/Pyotr name forms in a line, token-level."""
    out = []
    toks = line_tokens(ln)
    for i, tk in enumerate(toks):
        t = strip_digits(tk["norm"])
        m = match_name_token(t)
        if m:
            m["token_index"] = i
            m["token_raw"] = tk["raw"]
            m["trailing_digits"] = tk["norm"][len(t):] or None
            out.append(m)
    return out


def leading_relation(toks: list[dict]):
    """(role, consumed_upto_index) if the line starts with a relation word."""
    for i, tk in enumerate(toks[:3]):
        t = strip_digits(tk["norm"])
        if not t:
            continue
        r = match_relation_token(t)
        if r:
            role, raw = r
            j = i + 1
            while j < len(toks) and strip_digits(toks[j]["norm"]) in REL_TARGETS:
                j += 1
            return role, j
    return None, 0


def person_from_line(ln: dict, role: str, toks: list[dict],
                     rel_end: int) -> dict:
    """A person record from one OCR line (UNVERIFIED reading)."""
    alpha = [ (i, tk) for i, tk in enumerate(toks)
              if len(strip_digits(tk["norm"])) >= 2 ]
    given_idx = None
    if role in ("head", "widow"):
        for i, tk in alpha:
            if i >= rel_end:
                given_idx = i
                break
        if given_idx is None and alpha:
            given_idx = alpha[0][0]
    else:
        for i, tk in alpha:
            if i >= rel_end:
                given_idx = i
                break
    given = dict(toks[given_idx]) if given_idx is not None else None
    patr = None
    if given_idx is not None:
        for i, tk in alpha:
            if i > given_idx:
                t = strip_digits(tk["norm"])
                if t in REL_TARGETS or t in STATUS_TOKENS or len(t) < 3:
                    if t in STATUS_TOKENS:
                        break  # status marker ends the name span
                    continue  # «его/их/из…» glue, not a patronymic
                patr = dict(tk)
                break
    # status words («вдов», «холост») as second token -> status not patronymic
    status = None
    if patr is None:
        for i, tk in alpha:
            if given_idx is not None and i > given_idx and \
                    strip_digits(tk["norm"]) in STATUS_TOKENS:
                status = tk["raw"]
                break
    gm = match_name_token(strip_digits(given["norm"])) if given else None
    pm = match_name_token(strip_digits(patr["norm"])) if patr else None
    if gm and gm["form"] != "given":
        gm = None
    if pm and pm["form"] != "patronymic":
        pm = None
    inline_age = None
    if ln["text_raw"]:
        m = re.search(r"(\d{1,3})\s*[.,]?\s*$", ln["text_raw"])
        if m and int(m.group(1)) <= 120:
            inline_age = int(m.group(1))
    inline_chislo = None
    m = re.match(r"\s*(\d{1,4})\b", ln["text_raw"])
    if m:
        inline_chislo = m.group(1)
    return {
        "role": role,
        "name_ocr": " ".join(tk["raw"] for _, tk in alpha[:3]
                             if tk is not None) or "—",
        "given_raw": given["raw"] if given else None,
        "given_match": gm,
        "patronymic_raw": patr["raw"] if patr else None,
        "patronymic_match": pm,
        "status_ocr": status,
        "inline_age": inline_age,
        "inline_chislo": inline_chislo,
        "line_id": ln["line_id"],
        "line_number": ln["line_number"],
        "bbox": ln["bbox"],
        "ocr_confidence": ln["confidence"],
    }


# ------------------------------------------------------ segmentation ----
def segment_column(plines: list[dict], page: dict) -> tuple[list[dict], dict]:
    """Households within one register page (column).

    Returns (households, layout) where layout carries the number-line bands.
    Persons keep refs to their lines; boundaries flagged uncertain where OCR
    makes the structure ambiguous (never silently resolved).
    """
    persons_lines = [l for l in plines if any(
        len(strip_digits(normalize(t))) >= 2 for t in l["text_raw"].split())]
    if not persons_lines:
        return [], {}
    name_x = sorted(l["xc"] for l in persons_lines)[len(persons_lines) // 2]
    households: list[dict] = []
    prev_person = None
    in_children = False
    for ln in plines:
        toks = line_tokens(ln)
        alpha = [tk for tk in toks if len(strip_digits(tk["norm"])) >= 2]
        if not alpha:
            # number-only / empty line: handled when attaching to persons
            continue
        if is_header_line([tk["norm"] for tk in alpha]):
            continue
        tn = ln["text_norm"]
        kind = heading_kind(tn)
        if kind in ("village", "year", "closing") and "жена" not in tn \
                and "сын" not in tn:
            households.append({"kind": "heading", "heading": kind,
                               "village_ocr": (extract_village_name(
                                   [tk["norm"] for tk in alpha])
                                   if kind == "village" else None),
                               "line": ln})
            prev_person, in_children = None, False
            continue
        role, rel_end = leading_relation(toks)
        if role is None:
            # head-candidate; children-continuation heuristics
            n_alpha = len(alpha)
            second = strip_digits(alpha[1]["norm"]) if n_alpha > 1 else ""
            is_bare = n_alpha == 1 or second in STATUS_TOKENS
            if prev_person is not None and in_children and is_bare:
                role = "child-continuation"
            else:
                role = "head"
        if role == "widower-mark":
            role = "head"
        p = person_from_line(ln, role, toks,
                             rel_end if role != "head" else 0)
        if role in ("child-continuation",):
            p["role"] = "child"
            p["boundary_uncertain"] = True
        # a widow/widower line starts a NEW household (widowed heads);
        # garbled head lines make the boundary uncertain
        starts_new = role == "head" or role == "widow"
        if role in ("head", "widow"):
            in_children = False
        if not households or households[-1]["kind"] == "heading" \
                or starts_new:
            households.append({"kind": "household", "persons": [],
                               "first_line": ln, "lines": [],
                               "boundary_uncertain": False})
        if role == "head" and not p.get("given_match") \
                and not p.get("patronymic_match"):
            households[-1]["boundary_uncertain"] = True
        h = households[-1]
        if h["kind"] != "household":
            continue
        h["persons"].append(p)
        h["lines"].append(ln)
        if p["role"] == "children":
            in_children = True
        elif p["role"] == "head":
            in_children = False
        prev_person = p
    # attach number lines (chislo / age) by y-proximity
    numbers = [l for l in plines if l["text_norm"] and
               NUM_RE.match(l["text_norm"])]
    for h in households:
        if h["kind"] != "household":
            continue
        for p in h["persons"]:
            best_ch, best_age = None, None
            for n in numbers:
                dy = abs(n["yc"] - (p["bbox"][1] + p["bbox"][3] / 2.0))
                if dy > 30:
                    continue
                val = n["text_norm"].strip().strip(".,")
                if not val.isdigit():
                    continue
                if n["xc"] < name_x - 40:
                    if best_ch is None or dy < best_ch[0]:
                        best_ch = (dy, int(val), n)
                elif n["xc"] > name_x + 120 and int(val) <= 110:
                    if best_age is None or dy < best_age[0]:
                        best_age = (dy, int(val), n)
            if best_ch:
                p["chislo"] = best_ch[1]
            if p["inline_age"] is None and best_age:
                p["age"] = best_age[1]
            elif p["inline_age"] is not None and p["inline_age"] <= 110:
                p["age"] = p["inline_age"]
                p["age_source"] = "inline digits on name line (uncertain)"
            # flag possible OCR row gaps
        ys = [p["bbox"][1] for p in h["persons"]]
        for a, b in zip(ys, ys[1:]):
            if b - a > 60:
                h["boundary_uncertain"] = True
                h.setdefault("ocr_gap_flags", []).append(
                    f"y-gap {int(b - a)}px between rows — possible "
                    f"unread/missed OCR row")
    # households starting with a relation word = continuation from the
    # previous register page (merged later)
    for h in households:
        if h["kind"] == "household" and h["persons"] \
                and h["persons"][0]["role"] in (
                    "spouse", "son", "daughter", "children", "brother",
                    "sister", "foster"):
            h["starts_with_relation"] = True
    return households, {"name_x": name_x}


def build_case_flow(pages: dict, lines: dict):
    """(case -> ordered [(page_id, side, col_lines)]) register-flow order."""
    flow = defaultdict(list)
    for pid in sorted(pages, key=lambda p: (pages[p]["case_short"],
                                            pages[p]["page_number"])):
        p = pages[pid]
        cols = order_page_columns(pid, p, lines.get(pid, []))
        for side in ("L", "R"):
            if cols[side]:
                flow[p["case_short"]].append((pid, side, cols[side], p))
    return flow


def segment_corpus(pages: dict, lines: dict) -> dict:
    """All households, with cross-column continuation merges and
    settlement attribution via heading lookback."""
    flow = build_case_flow(pages, lines)
    out = {}
    for case, seq in flow.items():
        col_households = []
        col_layouts = []
        for pid, side, col, page in seq:
            hs, layout = segment_column(col, page)
            col_households.append((pid, side, hs))
            col_layouts.append((pid, side, layout))
        # merge continuations: a household starting with a relation word
        # attaches to the last household of the previous column (same case),
        # unless a heading intervened.
        merged: list[dict] = []
        last_hh = None
        for (pid, side, hs) in col_households:
            heading_since = False
            for h in hs:
                if h["kind"] == "heading":
                    heading_since = True
                    last_hh = None
                    merged.append(h)
                    continue
                if h.get("starts_with_relation") and last_hh is not None \
                        and not heading_since:
                    last_hh["persons"].extend(h["persons"])
                    last_hh["lines"].extend(h["lines"])
                    last_hh["merged_continuation"] = {
                        "from_page": pid, "from_side": side,
                        "note": ("household continues across a register-page "
                                 "boundary; merge is layout-based, flagged "
                                 "for human verification")}
                else:
                    h["page_id"] = pid
                    h["side"] = side
                    merged.append(h)
                    last_hh = h
        # settlement lookback
        cur_village = None
        cur_year_reset = False
        for h in merged:
            if h["kind"] == "heading":
                if h["heading"] in ("village",):
                    cur_village = h["village_ocr"] or None
                elif h["heading"] in ("year", "closing"):
                    cur_village = None
                continue
            h["village_ocr"] = cur_village
        out[case] = merged
    return out


# ------------------------------------------------------------ patterns ---
PATTERN_DESC = {
    "P1": "Иван is household head/father, Пётр explicitly his son "
          "(«сын/дети … Петръ» in Ivan's household)",
    "P2": "a person recorded as Пётр Ивановъ / Петръ Ивановичъ "
          "(patronymic form; father Ivan implied)",
    "P3": "Иван and Пётр in the same apparent household, relationship "
          "unresolved",
    "P4": "Пётр is father of Иван («Иванъ Петровъ» patronymic, or explicit "
          "son formula, or explicit brothers «брат его»)",
    "P5": "chronologically compatible Иван without Пётр in the household — "
          "possible father of a son born after the source year",
}


def classify_patterns(h: dict) -> tuple[list[str], list[str]]:
    """(patterns, evidence_strings) for one household."""
    pats, ev = [], []
    pers = h["persons"]
    ivans = [p for p in pers if p["given_match"]
             and p["given_match"]["name"] == "иван"
             and p["given_match"]["form"] == "given"
             and p["given_match"].get("strong")]
    petrs = [p for p in pers if p["given_match"]
             and p["given_match"]["name"] == "петр"
             and p["given_match"]["form"] == "given"
             and p["given_match"].get("strong")]
    petr_ivanych = [p for p in petrs if p["patronymic_match"]
                    and p["patronymic_match"]["name"] == "иван"
                    and p["patronymic_match"].get("strong")]
    ivan_petrovich = [p for p in ivans if p["patronymic_match"]
                      and p["patronymic_match"]["name"] == "петр"
                      and p["patronymic_match"].get("strong")]
    # P1: Ivan as household head (or widowed head) + explicit son/children
    # Pyotr.  Requires EXACTLY ONE strong-Иван person in the segment: with
    # two or more (e.g. widower Иван + «брат его Иванъ» with his own wife)
    # the son's father is ambiguous -> P3, never silently attributed.
    ivan_fathers = [p for p in ivans if p["role"] in ("head", "widow")]
    son_petrs = [s for s in petrs
                 if s["role"] in ("son", "children", "child")]
    if len(ivans) == 1 and ivan_fathers and son_petrs:
        f = ivan_fathers[0]
        for s in son_petrs:
            pats.append("P1")
            ev.append(f"«{f['given_raw']}» ({f['role']}) with "
                      f"«{s['given_raw']}» recorded as {s['role']} "
                      f"(son/children) — explicit father–son formula")
            break
    elif len(ivans) >= 2 and son_petrs:
        names = ", ".join(p["given_raw"] or "?" for p in ivans)
        pats.append("P3")
        ev.append(f"son «{son_petrs[0]['given_raw']}» present but "
                  f"{len(ivans)} Иван-persons in the segment ({names}) — "
                  f"father ambiguous, NOT attributed")
    # P2: Pyotr + Иванов patronymic
    for p in petr_ivanych:
        pats.append("P2")
        ev.append(f"«{p['given_raw']} {p['patronymic_raw'] or ''}» — "
                  f"Пётр Ивановъ-type entry (patronymic implies father "
                  f"Иван; father may be alive, dead, or absent from this "
                  f"list)")
    # P4a: Ivan + Петров patronymic  /  explicit brothers
    for p in ivan_petrovich:
        pats.append("P4")
        ev.append(f"«{p['given_raw']} {p['patronymic_raw'] or ''}» — "
                  f"Иван Петровъ-type entry (father Пётр implied)")
    for a in ivans:
        for b in petrs:
            if a is b:
                continue
            if a["role"] == "brother" and b["role"] in ("head", "widow",):
                pats.append("P4")
                ev.append(f"«{a['given_raw']}» recorded as брат of head "
                          f"«{b['given_raw']}» — explicit brothers")
            if b["role"] == "brother" and a["role"] in ("head", "widow"):
                pats.append("P4")
                ev.append(f"«{b['given_raw']}» recorded as брат of head "
                          f"«{a['given_raw']}» — explicit brothers")
            if a["role"] in ("son", "children", "child") \
                    and b["role"] in ("son", "children", "child"):
                pats.append("P4")
                ev.append(f"«{a['given_raw']}» and «{b['given_raw']}» both "
                          f"recorded as children of this household — "
                          f"explicit brothers (verify same parents)")
    # P4b: head Pyotr + explicit son Ivan (Пётр father of Иван); single-
    # Пётр rule for the same ambiguity reason as P1
    petr_fathers = [p for p in petrs if p["role"] in ("head", "widow")]
    son_ivans = [s for s in ivans
                 if s["role"] in ("son", "children", "child")]
    if len(petrs) == 1 and petr_fathers and son_ivans:
        f = petr_fathers[0]
        pats.append("P4")
        ev.append(f"head «{f['given_raw']}» with «{son_ivans[0]['given_raw']}» "
                  f"recorded as {son_ivans[0]['role']} — explicit "
                  f"Пётр-father/Иван-son formula")
    elif len(petrs) >= 2 and son_ivans:
        names = ", ".join(p["given_raw"] or "?" for p in petrs)
        pats.append("P3")
        ev.append(f"son «{son_ivans[0]['given_raw']}» present but "
                  f"{len(petrs)} Пётр-persons in the segment ({names}) — "
                  f"father ambiguous, NOT attributed")
    # P3: co-residence unresolved
    if ivans and petrs and "P1" not in pats and "P4" not in pats:
        pats.append("P3")
        ev.append("Иван and Пётр persons in the same household segment; "
                  "no explicit formula links them (OCR or layout unresolved)")
    # P5: Ivan without any Pyotr
    if ivans and not petrs and not any(
            p["patronymic_match"] and p["patronymic_match"]["name"] == "петр"
            for p in pers):
        pats.append("P5")
        ev.append("Иван present, no Пётр person and no Петровъ-form in this "
                  "household segment — a son Пётр born after the source "
                  "year is chronologically possible")
    return list(dict.fromkeys(pats)), ev


# -------------------------------------------------------------- ranking ---
def rank_components(h: dict, pat: str, page_year: dict | None,
                    recurrence: dict | None) -> tuple[float, dict]:
    base = {"P1": 40.0, "P4": 38.0, "P2": 38.0, "P3": 26.0, "P5": 14.0}[pat]
    comps = {"pattern_base": base}
    # a strong patronymic form IS relationship evidence (implied father)
    patr_strong = [p for p in h["persons"] if p["patronymic_match"]
                   and p["patronymic_match"].get("strong")]
    comps["patronymic_evidence"] = min(len(patr_strong), 2) * 4.0
    sec = h.get("section")
    comps["settlement"] = (14.0 if sec and sec["classification"] == "confirmed"
                           else 9.0 if sec else 0.0)
    key = [p for p in h["persons"] if p["given_match"]]
    exact = sum(1 for p in key if p["given_match"].get("strong"))
    fuzzy = sum(1 for p in key if not p["given_match"].get("strong"))
    comps["exact_name_forms"] = min(exact, 3) * 12.0
    comps["weak_fuzzy_name_forms"] = min(fuzzy, 3) * 2.0
    # age compatibility
    ages = [(p, p.get("age")) for p in h["persons"] if p.get("age")]
    comp_age, note = 0.0, None
    if pat in ("P1", "P4") and len(ages) >= 2:
        # explicit pair: father vs son
        f_age = s_age = None
        if pat == "P1":
            f = next((p for p in h["persons"] if p["given_match"]
                      and p["given_match"]["name"] == "иван"
                      and p["role"] in ("head", "widow")), None)
            s = next((p for p in h["persons"] if p["given_match"]
                      and p["given_match"]["name"] == "петр"
                      and p["role"] in ("son", "children", "child")), None)
            f_age, s_age = (f or {}).get("age"), (s or {}).get("age")
        else:
            f = next((p for p in h["persons"] if p["given_match"]
                      and p["given_match"]["name"] == "петр"
                      and p["role"] in ("head", "widow")), None)
            s = next((p for p in h["persons"] if p["given_match"]
                      and p["given_match"]["name"] == "иван"
                      and p["role"] in ("son", "children", "child")), None)
            f_age, s_age = (f or {}).get("age"), (s or {}).get("age")
        if f_age and s_age:
            if 16 <= f_age - s_age <= 55:
                comp_age, note = 8.0, f"ages {f_age}/{s_age} compatible"
            else:
                comp_age, note = -10.0, (
                    f"ages {f_age}/{s_age} INCOMPATIBLE with the claimed "
                    f"father–son relation — contradiction flagged")
    elif pat == "P5":
        iv = next((p for p in h["persons"] if p["given_match"]
                   and p["given_match"]["name"] == "иван"), None)
        if iv and iv.get("age"):
            comp_age = 6.0 if 16 <= iv["age"] <= 60 else 0.0
            note = f"Иван age {iv['age']}"
    comps["age_compatibility"] = comp_age
    if note:
        comps["age_note"] = note
    # corroborating members
    corr = [p for p in h["persons"] if p["role"] in (
        "spouse", "son", "daughter", "children", "brother", "sister",
        "foster") and p.get("given_raw")]
    comps["corroborating_members"] = min(len(corr), 3) * 2.0
    comps["recurrence_across_years"] = 8.0 if recurrence else 0.0
    conf = [p["ocr_confidence"] for p in h["persons"]
            if p["ocr_confidence"] is not None]
    comps["ocr_confidence_hint"] = (round(3.0 * sum(conf) / len(conf), 1)
                                    if conf else 0.0)
    total = round(sum(v for k, v in comps.items()
                      if isinstance(v, (int, float))), 1)
    return total, comps


# ---------------------------------------------------------------- main ---
def main() -> int:
    t_start = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CROP_DIR.mkdir(parents=True, exist_ok=True)
    conn, db = load_db()
    pages, lines = db["pages"], db["lines"]
    searcher = Searcher()
    sconn = searcher.conn
    sconn.row_factory = sqlite3.Row

    pool, pool_stats = build_pool(sconn, searcher)
    print(f"pool: {pool_stats}")

    page_year, year_events = year_runs(pages, lines)

    # ---- full-corpus token classification (complete counts; the pool is
    # the cross-check that the FTS/trigram recall layer loses nothing)
    all_name_lines = {}
    for pid, plines in lines.items():
        for ln in plines:
            if not ln["text_norm"]:
                continue
            hits = classify_line(ln)
            if hits:
                all_name_lines[ln["line_id"]] = hits
    pool_miss = [lid for lid in pool if lid not in all_name_lines]

    # ---- totals (distinguished given vs patronymic-only)
    def totals_for(name_key: str) -> dict:
        given_exact = given_fuzzy = given_fuzzy_weak = 0
        patr_only = patr_total = 0
        by_case = defaultdict(lambda: Counter())
        for lid, hits in all_name_lines.items():
            pid = None
            for h in hits:
                if h["name"] != name_key:
                    continue
                if pid is None:
                    pid = conn.execute(
                        "SELECT page_id, case_short FROM lines l JOIN pages "
                        "p USING(page_id) WHERE l.line_id=?",
                        (lid,)).fetchone()
                c = pid["case_short"]
                if h["form"] == "given":
                    if h["tier"] == "fuzzy":
                        if h.get("strong"):
                            given_fuzzy += 1
                            by_case[c]["given_fuzzy_strong"] += 1
                        else:
                            given_fuzzy_weak += 1
                            by_case[c]["given_fuzzy_weak"] += 1
                    else:
                        given_exact += 1
                        by_case[c]["given_exact"] += 1
                else:
                    patr_total += 1
                    by_case[c]["patronymic_forms"] += 1
        # patronymic-only lines: form present, no given form of same name
        patr_only_by_case = Counter()
        for lid, hits in all_name_lines.items():
            if any(h["name"] == name_key and h["form"] == "given"
                   for h in hits):
                continue
            if any(h["name"] == name_key and h["form"] == "patronymic"
                   for h in hits):
                patr_only += 1
                r = conn.execute("SELECT case_short FROM lines l JOIN "
                                 "pages p USING(page_id) WHERE l.line_id=?",
                                 (lid,)).fetchone()
                patr_only_by_case[r["case_short"]] += 1
        return {
            "given_name_lines_exact_or_glued": given_exact,
            "given_name_lines_fuzzy_strong_80plus": given_fuzzy,
            "given_name_lines_fuzzy_weak_74_79": given_fuzzy_weak,
            "fuzzy_note": ("fuzzy = OCR-damaged candidate forms; weak "
                           "(74-79 similarity) are mostly non-name noise "
                           "retained as leads"),
            "patronymic_form_occurrences": patr_total,
            "patronymic_only_lines": patr_only,
            "patronymic_only_by_case": dict(patr_only_by_case),
            "given_by_case": {c: dict(v) for c, v in by_case.items()},
        }

    totals = {
        "lines_indexed": sum(len(v) for v in lines.values()),
        "pages": len(pages),
        "pool_recall": pool_stats,
        "pool_lines_without_token_match": len(pool_miss),
        "pool_miss_note": (
            "pool cross-check: FTS5+trigram recall lines that the full "
            "token pass did NOT classify (expected small: substring matches "
            "inside non-name words like «дивана», plus glued digits)"),
        "иван": totals_for("иван"),
        "пётр": totals_for("петр"),
    }

    # ---- households + settlement + section
    households_by_case = segment_corpus(pages, lines)
    groups = []
    for case, hs in households_by_case.items():
        for h in hs:
            if h["kind"] != "household":
                continue
            has_name = any(p["given_match"] for p in h["persons"])
            if not has_name:
                continue
            # section attribution per household (by its page/side/first y)
            pid, side = h["page_id"], h["side"]
            sec = in_section(case, pid, side,
                             h["first_line"]["yc"])
            if sec is None and "merged_continuation" in h:
                sec = in_section(case, pid, side,
                                 h["first_line"]["yc"])  # spans cover pages
            h["section"] = ({"label": sec["label"],
                             "classification": sec["cls"]} if sec else None)
            if sec is None:  # any line of the household inside a section?
                for ln in h["lines"][1:]:
                    s2 = in_section(case, pid, side, ln["yc"])
                    if s2:
                        h["section"] = {"label": s2["label"],
                                        "classification": s2["cls"],
                                        "partial": True}
                        break
            h["case"] = case
            groups.append(h)

    # ---- patterns per group
    for h in groups:
        pats, ev = classify_patterns(h)
        h["patterns"] = pats
        h["relation_evidence"] = ev

    # ---- context ±8 (column-aware) + occurrence inventory
    for h in groups:
        pid, side = h["page_id"], h["side"]
        col = order_page_columns(pid, pages[pid], lines.get(pid, []))[side]
        idxs = [i for i, l in enumerate(col) if l in h["lines"]]
        if not idxs:
            h["context"] = []
            continue
        lo, hi = min(idxs), max(idxs)
        lo2, hi2 = max(0, lo - CONTEXT_K), min(len(col), hi + CONTEXT_K + 1)
        h["context"] = [context_entry(l, h) for l in col[lo2:hi2]]
        h["context_window"] = [lo2, hi2]

    # ---- year per group
    for h in groups:
        pid = h["page_id"]
        py = page_year.get(pid)
        lo, hi = CASE_RANGE[h["case"]]
        if py and py.get("value"):
            y = py["value"]
            in_range = lo <= y <= hi
            h["archival_year"] = {
                "value": y,
                "case_range": f"{lo}-{hi}",
                "method": py["method"] + f"; token on {py['evidence_page']}",
                "status": ("OCR-derived, UNVERIFIED; token year is OUTSIDE "
                           "the case archive range — reading suspect, do "
                           "not rely on it" if not in_range else
                           "OCR-derived, UNVERIFIED")}
        else:
            h["archival_year"] = {
                "value": None, "range": f"{lo}-{hi}",
                "status": "year token not OCR-readable near this page; "
                          "case archive range shown"}

    # ---- recurrence (flagged lead, NEVER identity merge)
    def sig(h):
        head = next((p for p in h["persons"]
                     if p["role"] in ("head", "widow") and p["given_match"]),
                    None)
        mems = tuple(sorted(p["given_match"]["stem"] for p in h["persons"]
                      if p["given_match"] and p["role"] not in ("head", "widow")
                      and p["given_match"]["form"] == "given"))
        patr = tuple(sorted(p["patronymic_match"]["stem"]
                      for p in h["persons"] if p["patronymic_match"]))
        head_patr = normalize(head["patronymic_raw"] or "") if head else ""
        return (head["given_match"]["stem"] if head else None,
                mems, patr, head_patr)

    def head_patr_similar(a, b) -> bool:
        pa, pb = a[3], b[3]
        if not pa or not pb:
            return False
        return fuzz.ratio(pa, pb) >= 72

    by_sig = defaultdict(list)
    for h in groups:
        s = sig(h)
        # a signature with no members, no patronymics and no head patronymic
        # is a bare «Иванъ» line — never treated as recurrence evidence
        if s[0] and (s[1] or s[2] or s[3]):
            by_sig[(s[0], s[1], s[2])].append(h)
    for h in groups:
        s = sig(h)
        if not (s[1] or s[2] or s[3]):
            continue
        partners = [g for g in by_sig.get((s[0], s[1], s[2]), [])
                    if g is not h
                    and (g["case"] != h["case"]
                         or (g.get("section") or {}).get("label")
                         != (h.get("section") or {}).get("label"))
                    and (not s[3] or head_patr_similar(s, sig(g)))]
        if partners:
            h["recurrence"] = {
                "note": ("same settlement section + same head/member name "
                         "signature appears in another case/section — "
                         "POSSIBLE recurrence across years; NOT an identity "
                         "assertion; human review required"),
                "partner_groups": [group_id(g) for g in partners[:4]],
            }

    # ---- score + rank (primary pattern = strongest documented)
    pat_order = {"P1": 0, "P4": 1, "P2": 2, "P3": 3, "P5": 4}
    ranked = []
    for h in groups:
        if not h["patterns"]:
            continue  # patronymic-only households without given names: no
            # Ivan/Pyotr PERSON — still counted in totals, not a group
        prim = sorted(h["patterns"], key=lambda p: pat_order[p])[0]
        sc, comps = rank_components(h, prim, page_year.get(h["page_id"]),
                                    h.get("recurrence"))
        h["primary_pattern"] = prim
        h["score"] = sc
        h["score_components"] = comps
        ranked.append(h)
    ranked.sort(key=lambda h: -h["score"])

    # counts of distinct groups per pattern (a group may carry several)
    pat_counts = Counter()
    for h in ranked:
        for p in h["patterns"]:
            pat_counts[p] += 1

    # person-level estimates
    def person_counts(name_key):
        n_exact = n_fuzzy = 0
        for h in groups:
            for p in h["persons"]:
                m = p["given_match"]
                if m and m["name"] == name_key and m["form"] == "given":
                    if m["tier"] == "fuzzy":
                        n_fuzzy += 1
                    else:
                        n_exact += 1
        return {"estimated_persons_exact": n_exact,
                "estimated_persons_fuzzy": n_fuzzy,
                "note": "heuristic household segmentation; boundary "
                        "uncertainty flags included per group"}

    # ---- export
    top = ranked[:TOP_N_REPORT]
    export_groups = []
    for i, h in enumerate(top, 1):
        export_groups.append(group_export(h, i, pages))
    payload = {
        "task": "KINSHIP-SEARCH-02 — Ivan/Pyotr contextual family search",
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_s": round(time.time() - t_start, 1),
        "archive": ARCHIVE,
        "cases": {"253": "1813-19", "255": "1820-24", "256": "1825-29",
                  "258": "1830-34"},
        "scope_note": (
            "Targeted search over the existing OCR index; no corpus OCR "
            "rerun, no new models. Retrieval not limited to Волченокъ: "
            "confirmed/probable Волченокъ sections are prioritized in "
            "ranking, candidates from other settlements retained."),
        "method": {
            "recall": ("SQL-prefix scan (complete exact-stem counts) ∪ "
                       "FTS5 prefix ∪ trigram corruption net (existing "
                       "Searcher stack) ∪ full-corpus token pass with "
                       "RapidFuzz (cross-check layer)"),
            "classification": ("given-name forms distinguished from "
                               "patronymic forms at token level; «Иван "
                               "Петровъ» counts as given-Иван + "
                               "patronymic-Петровъ and is NOT treated as "
                               "«Пётр Ивановъ»; raw OCR forms preserved and "
                               "flagged, never silently corrected"),
            "context": ("±8 lines per household within the same register "
                        "page column (each scan = two register pages, split "
                        "at page mid-x); overlapping windows deduplicated "
                        "into household groups; cross-page continuations "
                        "merged with an explicit flag"),
            "patterns": PATTERN_DESC,
            "ranking": ("documented relationship > patronymic > "
                        "co-residence > absence; exact personal-name above "
                        "fuzzy; confirmed Волченокъ above probable above "
                        "other settlements; age compatibility and "
                        "corroborating members add; recurrence across years "
                        "is a flagged lead (+8) and NEVER an identity merge; "
                        "landowner surnames (Головкина, Шуваловы, "
                        "Трубниковы …) are owner context only and never "
                        "count as family evidence"),
            "verification_status": (
                "ALL name readings are UNVERIFIED OCR leads; households are "
                "heuristic; nothing here is a verified person; no "
                "connection to any established genealogy is made"),
        },
        "totals": totals,
        "person_estimates": {"иван": person_counts("иван"),
                             "пётр": person_counts("петр")},
        "pattern_group_counts": dict(pat_counts),
        "distinct_candidate_groups": len(ranked),
        "groups_in_volchenok_sections": sum(
            1 for h in ranked if h.get("section")),
        "top_groups": export_groups,
        "conclusion": {
            "manual_examination_first": [
                {"priority": 1,
                 "group_ids": ["258-p011R-g46165", "258-p065R-g55351"],
                 "why": ("widower «Иванъ Ев(е/а)гнеевъ/Евстигнеевъ» with "
                         "«жена его Васса», children Праскева, ПЕТРЪ, "
                         "Ольга, Тимофей, plus «Ивановъ братъ Иванъ вдовъ» "
                         "(own son Стефанъ). Two Иван-persons: which of "
                         "them fathered Пётр is unresolved -> read the "
                         "scans. The same signature appears in ~1831 "
                         "(258-p011) and ~1834 (258-p065) — cross-year "
                         "recurrence flagged, identity NOT asserted. "
                         "AI-assisted visual re-read of the p011 region "
                         "confirms the two-Ivan structure.")},
                {"priority": 2,
                 "group_ids": ["258-p029R-g49137"],
                 "why": ("single-Иван segment: widower «Иванъ Евстифеев-"
                         "форм» with child «Петръ» (P1). AI-assisted "
                         "visual re-read confirms «Иванъ … вдовъ» and a "
                         "«Петръ» child row; verify the adjacent rows "
                         "(a second Иван line may exist at the crop "
                         "edge).")},
                {"priority": 3,
                 "group_ids": ["253-p058L-g09891"],
                 "why": ("clean P2 entry: «Петръ Ивановъ, холостъ» — "
                         "patronymic documents an Иван of the previous "
                         "generation even if that Иван is not in this "
                         "list; the only clean Пётр Ивановъ form found.")},
                {"priority": 4,
                 "group_ids": ["258-p061R-g54587"],
                 "why": ("«Нагаръ Петровъ» (implied father Пётр) with sons "
                         "«Иванъ» and «Петръ» — a three-generation "
                         "Пётр→Нагаръ→Иван/Пётр chain in one household "
                         "(деревня Тимофеева, ~1834).")},
                {"priority": 5,
                 "group_ids": ["258-p016L-g47016", "258-p070R-g56320",
                               "253-p081R-g14461", "253-p067R-g11791"],
                 "why": ("widower «Иванъ Прок(опи)евъ» with son «Иванъ» in "
                         "CONFIRMED Волченокъ sections (258-A ~1831, "
                         "258-C ~1833-34) and probable 253-A (~1813-19), "
                         "plus a further «иван прокопевь вдрвь» at "
                         "253-p067 — cross-year/cross-case recurrence "
                         "flagged. A son "
                         "Пётр born after any of these list-years is "
                         "chronologically possible; linking the years is "
                         "a manual job (no identity asserted).")},
            ],
            "summary": (
                f"Ivan/Pyotr father-son DOCUMENTATION is rare in this OCR: "
                f"{pat_counts.get('P1', 0)} P1, {pat_counts.get('P4', 0)} "
                f"P4, {pat_counts.get('P2', 0)} P2 groups carry an "
                f"explicit formula or patronymic; "
                f"{pat_counts.get('P3', 0)} P3 groups are co-residence "
                f"with ambiguous attribution; {pat_counts.get('P5', 0)} "
                f"P5 groups are the Ivan-without-Pyotr pool (ranked: "
                f"confirmed Волченокъ sections first). The "
                f"{totals['иван']['patronymic_only_lines']}/"
                f"{totals['пётр']['patronymic_only_lines']} "
                f"patronymic-only lines (Ивановъ/Петровъ without the "
                f"given name) are counted separately and imply many more "
                f"unrecorded fathers, but they are NOT person occurrences. "
                f"Landowner surnames were excluded from all kinship "
                f"evidence. Everything above awaits human paleographic "
                f"verification."),
        },
        "all_groups_brief": [{
            "group_id": group_id(h), "patterns": h["patterns"],
            "score": h["score"],
            "case": h["case"], "page": pages[h["page_id"]]["page_number"],
            "section": h.get("section"),
            "village_ocr": h.get("village_ocr"),
            "persons_brief": [
                f"{p['role']}:{p['name_ocr']}"
                + (f"@{p['age']}" if p.get("age") else "")
                for p in h["persons"]],
        } for h in ranked],
    }
    (OUT_DIR / "kinship-search02.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"exported {len(export_groups)} top groups; "
          f"{len(ranked)} groups total; "
          f"pool lines w/o token match: {len(pool_miss)}")
    for h in ranked[:10]:
        print(f"  {h['score']:5.1f} {'/'.join(h['patterns'])} "
              f"{group_id(h)} «{h['persons'][0]['name_ocr'][:40]}»")
    searcher.close()
    conn.close()
    return 0


def group_id(h: dict) -> str:
    return f"{h['case']}-{h['page_id'].split('-')[1]}{h['side']}-g{min(p['line_id'] for p in h['persons']):05d}"


def context_entry(l: dict, h: dict) -> dict:
    tn = normalize(l["text_raw"])
    in_hh = l in h["lines"]
    tags = []
    if in_hh:
        tags.append("household")
    if heading_kind(tn):
        tags.append(heading_kind(tn))
    if NUM_RE.match(tn) and tn:
        tags.append("number")
    if not tn:
        tags.append("empty-ocr")
    return {"line_id": l["line_id"], "line_number": l["line_number"],
            "text_raw": l["text_raw"] or "",
            "bbox": l["bbox"], "tags": tags,
            "ocr_confidence": l["confidence"]}


def group_export(h: dict, rank: int, pages: dict) -> dict:
    pid = h["page_id"]
    p = pages[pid]
    uncs = []
    if h.get("boundary_uncertain"):
        uncs.append("household boundary uncertain (OCR gaps/ambiguity)")
    for f in h.get("ocr_gap_flags", []):
        uncs.append(f)
    if h.get("merged_continuation"):
        uncs.append("merged across register-page boundary — verify")
    for pr in h["persons"]:
        if pr["given_match"] and pr["given_match"]["tier"] == "fuzzy":
            uncs.append(f"fuzzy OCR name «{pr['given_raw']}» (score "
                        f"{pr['given_match'].get('fuzz')}) — verify image")
        if pr.get("age_source"):
            uncs.append(f"age for «{pr['name_ocr']}» from inline digits "
                        f"(uncertain)")
        a, r = pr.get("age"), pr.get("role")
        if a is not None and ((r in ("head", "widow") and a < 14)
                              or (r == "spouse" and a < 13)):
            uncs.append(f"age {a} on «{pr['name_ocr']}» ({r}) is "
                        f"implausible — likely a misattached number line; "
                        f"value kept as OCR shows it, verify image")
        if pr.get("chislo") is not None and pr["chislo"] < 5 \
                and r in ("head", "widow"):
            uncs.append(f"chislo {pr['chislo']} on «{pr['name_ocr']}» "
                        f"suspect (stray small number)")
    persons = []
    for pr in h["persons"]:
        persons.append({
            "role": pr["role"],
            "name_ocr": pr["name_ocr"],
            "given_raw_ocr": pr["given_raw"],
            "given_match": pr["given_match"],
            "patronymic_raw_ocr": pr["patronymic_raw"],
            "patronymic_match": pr["patronymic_match"],
            "age": pr.get("age"),
            "chislo": pr.get("chislo"),
            "status_ocr": pr.get("status_ocr"),
            "ocr_confidence": pr["ocr_confidence"],
            "bbox": pr["bbox"],
            "line_id": pr["line_id"],
            "line_number": pr["line_number"],
        })
    key_lines = [pr["line_id"] for pr in h["persons"] if pr["given_match"]]
    return {
        "rank": rank,
        "group_id": group_id(h),
        "patterns": h["patterns"],
        "primary_pattern": h["primary_pattern"],
        "case": h["case"],
        "page_id": pid,
        "page_number": p["page_number"],
        "column": h["side"],
        "archival_year": h["archival_year"],
        "settlement": {
            "section": h.get("section"),
            "village_ocr": h.get("village_ocr"),
            "note": ("settlement from visually verified section spans "
                     "(Волченокъ) or from the nearest OCR-readable village "
                     "heading (UNVERIFIED)") if not h.get("section")
                    else "visually verified Волченокъ section span",
        },
        "score": h["score"],
        "score_components": h["score_components"],
        "relation_evidence": h["relation_evidence"],
        "persons": persons,
        "context_lines": h.get("context", []),
        "uncertainties": uncs,
        "recurrence": h.get("recurrence"),
        "landowner_note": ("owner mentions (Головкина/Шуваловы/Трубниковы…)"
                            " in nearby headings are landowner context "
                            "only, never kinship evidence"),
        "links": {
            "full_page_image": f"/api/image/{pid}",
            "crop_first_key_line": (f"/api/crop/{key_lines[0]}?pad=200"
                                    if key_lines else None),
            "line_api": [f"/api/line/{lid}" for lid in key_lines[:3]],
            "source_image": p["image_path"],
        },
    }


if __name__ == "__main__":
    sys.exit(main())
