#!/usr/bin/env python3
"""KINSHIP-HTR-SEARCH-01: systematic genealogical name search over the OCR index.

For each name profile (Егоров, Царев, Головкин, Демидов, Мефодиев + the
lower-priority Иванов/Петров/Симеонов):
  * exact (FTS), fuzzy (trigram + RapidFuzz) and OCR-corrupted variant search;
  * historical spellings are preserved for display; normalized forms are used
    for retrieval only;
  * two-tier classification: strong = the line text contains a documented
    spelling or generated inflection (or >=90 similarity on a line long
    enough to carry the name); fuzzy = corruption-variant recall;
  * matches inside деревня Волченокъ sections (work/discovery/
    volchenok-sections.json) rank above unrelated occurrences;
  * each candidate carries household context: +/-6 lines in reading order
    with relation-word and numeric-age markers, for human review;
  * -ов/-ева forms are annotated patronymic-OR-surname: in 1813-34 peasant
    confession registers patronymics dominate, but the distinction needs a
    human reading of the household; NO automatic identity merging.

Nothing here is a verified person or kinship fact.  Every candidate is an
UNVERIFIED OCR lead for the review UI.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/mysterx/dev/kinship-htr-bench")

from rapidfuzz import fuzz  # noqa: E402

from htr_search.normalize import normalize  # noqa: E402
from htr_search.profiles import PROFILES, profile_variants  # noqa: E402
from htr_search.search import Searcher  # noqa: E402

BENCH = Path("/Users/mysterx/dev/kinship-htr-bench")
OUT_DIR = BENCH / "work" / "discovery" / "names"
SECTIONS_PATH = BENCH / "work" / "discovery" / "volchenok-sections.json"
TOP_N = 50
FUZZY_MIN_LEN = 6
LOW_PRIORITY = ("Иванов", "Петров", "Симеонов")

RELATION_RE = re.compile(
    r"жена|жену|женит|сын|дочь|дети|дти|брат|брата|внук|отец|матери|ихъ|его")
NUMERIC_RE = re.compile(r"^[\d\s.,:;]+$")
AGE_HINT_RE = re.compile(r"\d{1,3}")
PATRONYMIC_SUFFIX_RE = re.compile(r"(ов|ев|ин|ова|ева|ина|ских)$")


def load_sections() -> list[dict]:
    return json.loads(SECTIONS_PATH.read_text())["sections"]


def section_for(case: str, page: int, sections: list[dict]):
    for i, s in enumerate(sections):
        if s["case"] == case and s["page_range"][0] <= page <= s["page_range"][1]:
            return {"index": i, "status": s["status"],
                    "range": s["page_range"]}
    return None


def household_context(conn, page_id: str, line_number: int, k: int = 6):
    rows = conn.execute(
        "SELECT line_id, line_number, text_raw, bbox, y_center "
        "FROM lines WHERE page_id=? ORDER BY y_center, reading_order",
        (page_id,)).fetchall()
    idx = next((i for i, r in enumerate(rows)
                if r["line_number"] == line_number), None)
    if idx is None:
        return [], None
    lo, hi = max(0, idx - k), min(len(rows), idx + k + 1)
    ctx = []
    for j in range(lo, hi):
        r = rows[j]
        tn = normalize(r["text_raw"])
        ctx.append({
            "line_id": r["line_id"], "line_number": r["line_number"],
            "text_raw": r["text_raw"],
            "is_age_or_number_line": bool(NUMERIC_RE.match(tn.strip()) and tn),
            "has_relation_word": bool(RELATION_RE.search(tn)),
            "relation_to_candidate": "self" if j == idx else
            ("above" if j < idx else "below"),
        })
    return ctx, "self"


def patronymic_annotation(hit_text: str, ctx: list[dict]) -> dict:
    tn = normalize(hit_text)
    last = tn.split()[-1] if tn.split() else ""
    rel_in_line = bool(RELATION_RE.search(tn))
    ages = [c["text_raw"] for c in ctx if c["is_age_or_number_line"]]
    return {
        "form_class": ("patronymic-or-surname (suffix "
                       f"'{PATRONYMIC_SUFFIX_RE.search(last).group(0)}')"
                       if PATRONYMIC_SUFFIX_RE.search(last)
                       else "given-name-or-other"),
        "relation_words_in_line": rel_in_line,
        "age_or_number_lines_nearby": ages[:4],
        "interpretation_note": (
            "In 1813-34 peasant confession registers, -ов/-ев/-ина forms are "
            "usually PATRONYMICS, not proven hereditary surnames.  If the line "
            "reads like 'жена его <forename> <form>' the form is her maiden "
            "patronymic; if it follows a household head with the same form, "
            "it may be a family surname.  Deciding needs a human reading of "
            "the household; no identity merging is performed."),
    }


ANCHOR_MIN_SCORE = 75.0
ANCHOR_CONTEXT_K = 6
ANCHOR_MIN_CO = 2
ANCHOR_TOKEN_LIMIT = 3000


def anchor_leads(s, conn, name: str, sections: list[dict]) -> dict | None:
    """Household-anchor leads (KINSHIP-HTR-FAMILY-01 Phase A).

    When a surname is corrupted in the OCR beyond spelling-variant recall
    (e.g. Демидовъ -> «умитревъ» on 258-p014), the documented household
    co-names from the case transcript still retrieve the household.  A lead
    requires >= ANCHOR_MIN_CO distinct co-name matches (score >=
    ANCHOR_MIN_SCORE) within one ±ANCHOR_CONTEXT_K-line window on the same
    page.  Such a lead is evidence that a HUMAN should read that region —
    the matched lines contain no surname form in the OCR text.
    """
    anchors = PROFILES[name].get("household_anchors_documented")
    if not anchors:
        return None
    tokens = anchors["co_tokens"]
    per_line: dict[int, dict] = {}
    for tok in tokens:
        for h in s.search_lines(tok, limit=ANCHOR_TOKEN_LIMIT,
                                with_neighbors=False):
            if h.text_score < ANCHOR_MIN_SCORE:
                continue
            e = per_line.setdefault(h.line_id, {"hit": h, "tokens": {}})
            if h.text_score > e["tokens"].get(tok, 0.0):
                e["tokens"][tok] = h.text_score
    by_page: dict[str, list[dict]] = {}
    for e in per_line.values():
        by_page.setdefault(e["hit"].page_id, []).append(e)
    leads = []
    for pid, items in by_page.items():
        items.sort(key=lambda e: e["hit"].line_number)
        clusters, cur = [], [items[0]]
        for e in items[1:]:
            if (e["hit"].line_number
                    - cur[-1]["hit"].line_number <= ANCHOR_CONTEXT_K):
                cur.append(e)
            else:
                clusters.append(cur)
                cur = [e]
        clusters.append(cur)
        for cl in clusters:
            toks: dict[str, dict] = {}
            for e in cl:
                for tok, sc in e["tokens"].items():
                    c = toks.setdefault(tok, {"score": 0.0})
                    if sc > c["score"]:
                        c.update(score=sc, line_id=e["hit"].line_id,
                                 line_number=e["hit"].line_number,
                                 text_raw=e["hit"].text_raw,
                                 ocr_confidence=e["hit"].confidence)
            if len(toks) < ANCHOR_MIN_CO:
                continue
            page = conn.execute(
                "SELECT case_short, page_number FROM pages WHERE page_id=?",
                (pid,)).fetchone()
            sec = section_for(page["case_short"], page["page_number"],
                              sections)
            leads.append({
                "page_id": pid,
                "in_volchenok_section": bool(sec),
                "section": ({"status": sec["status"],
                             "page_range": sec["range"]} if sec else None),
                "line_span": [cl[0]["hit"].line_number,
                              cl[-1]["hit"].line_number],
                "matched_anchors": toks,
                "distinct_anchors": len(toks),
                "best_anchor_score": max(t["score"] for t in toks.values()),
                "provenance": (
                    "household-anchor lead: lines match co-names documented "
                    "in the case transcript, NOT a surname form; OCR text "
                    "on these lines contains no surname-like reading; "
                    "manual verification required"),
                "links": {
                    "full_page_image": f"/api/image/{pid}",
                    "crop_first_line":
                        f"/api/crop/{cl[0]['hit'].line_id}?pad=200",
                },
            })
    leads.sort(key=lambda L: (not L["in_volchenok_section"],
                              -L["distinct_anchors"],
                              -L["best_anchor_score"]))
    return {"tokens": tokens, "source": anchors["source"],
            "note": anchors["note"], "min_co_anchors": ANCHOR_MIN_CO,
            "min_score": ANCHOR_MIN_SCORE,
            "window_lines": ANCHOR_CONTEXT_K, "leads": leads}

def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sections = load_sections()
    s = Searcher()
    conn = s.conn

    for name in ("Егоров", "Царев", "Головкин", "Демидов", "Мефодиев",
                 "Иванов", "Петров", "Симеонов"):
        pv = profile_variants(name)
        doc_v = pv["documented"]
        inf_v = [normalize(v) for v in PROFILES[name]["inflections_generated"]]
        strong_v = doc_v + inf_v
        gen_v = pv["generated"]
        t0 = time.time()
        hits = s.search_lines(name, variants=pv["all"], limit=None,
                              with_neighbors=False)

        strong, fuzzy = [], []
        for h in hits:
            tn = normalize(h.text_raw)
            doc_score = max((fuzz.partial_ratio(v, tn) for v in doc_v),
                            default=0.0)
            gen_score = max((fuzz.partial_ratio(v, tn) for v in gen_v),
                            default=0.0)
            contained = any(v in tn for v in strong_v)
            h.doc_score, h.gen_score = doc_score, gen_score
            h.tier = ("strong" if contained or (doc_score >= 90
                        and len(tn) >= FUZZY_MIN_LEN) else "fuzzy")
            page = conn.execute(
                "SELECT case_short, page_number FROM pages WHERE page_id=?",
                (h.page_id,)).fetchone()
            h.sec = section_for(page["case_short"], page["page_number"],
                                sections)
            (strong if h.tier == "strong" else fuzzy).append(h)

        strong.sort(key=lambda h: (not bool(h.sec), -h.doc_score, -h.total))
        fuzzy.sort(key=lambda h: (not bool(h.sec), -h.total))
        top = (strong + fuzzy)[:TOP_N]

        pages: dict[str, dict] = {}
        rows = []
        for rank, h in enumerate(top, 1):
            ctx, _ = household_context(conn, h.page_id, h.line_number)
            rows.append({
                "rank": rank,
                "tier": h.tier,
                "in_volchenok_section": bool(h.sec),
                "section": ({"status": h.sec["status"],
                             "page_range": h.sec["range"]} if h.sec else None),
                "line_id": h.line_id,
                "page_id": h.page_id,
                "line_number": h.line_number,
                "source": h.source,
                "bbox": h.bbox,
                "text_raw": h.text_raw,
                "historical_spelling_note":
                    "displayed verbatim as OCR read it; retrieval used "
                    "normalized variants only",
                "ocr_confidence": h.confidence,
                "text_similarity_documented": round(h.doc_score, 1),
                "text_similarity_generated": round(h.gen_score, 1),
                "total_score": round(h.total, 1),
                "matched_variant": h.matched_variant,
                "household_context": ctx,
                "patronymic_vs_surname": patronymic_annotation(h.text_raw, ctx),
                "review": None,
                "links": {
                    "full_page_image": f"/api/image/{h.page_id}",
                    "crop_around_bbox": f"/api/crop/{h.line_id}?pad=160",
                    "line_api": f"/api/line/{h.line_id}",
                },
            })
            e = pages.setdefault(h.page_id, {"page_id": h.page_id,
                                             "in_section": bool(h.sec),
                                             "candidates": 0, "strong": 0})
            e["candidates"] += 1
            e["strong"] += (h.tier == "strong")

        # group repeated names by household (page + local cluster), no merging
        groups = []
        for h in top:
            if h.tier != "strong":
                continue
            key = (h.page_id, h.sec["status"] if h.sec else "outside-section")
            g = next((g for g in groups if g["page_id"] == h.page_id
                      and g["section_status"] == key[1]), None)
            if g is None:
                g = {"page_id": h.page_id, "section_status": key[1],
                     "line_numbers": []}
                groups.append(g)
            g["line_numbers"].append(h.line_number)
        anchor_payload = anchor_leads(s, conn, name, sections)
        in_sec = sum(1 for h in top if h.sec)
        payload = {
            "target": name,
            "priority": ("low — very common name; review inside Волченокъ "
                         "sections first" if name in LOW_PRIORITY else
                         "high"),
            "canonical": PROFILES[name]["canonical"],
            "documented_spellings_display": PROFILES[name]["documented"],
            "documented_note": PROFILES[name]["documented_note"],
            "generated_inflections": PROFILES[name]["inflections_generated"],
            "generated_corruptions_used": len(gen_v),
            "candidates_above_min_score": len(hits),
            "strong_candidates": len(strong),
            "fuzzy_candidates": len(fuzzy),
            "strong_in_volchenok_sections": sum(1 for h in strong if h.sec),
            "saved_top_n": len(rows),
            "top_n_in_sections": in_sec,
            "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "elapsed_s": round(time.time() - t0, 1),
            "scoring_note": (
                "text_similarity_documented = best partial_ratio against "
                "documented spellings (normalized for retrieval only; "
                "historical spelling preserved in text_raw). ocr_confidence "
                "is a separate field (capped hint in total_score). Ranking: "
                "strong tier above fuzzy; inside-Волченокъ above outside. "
                "household_context lines are a review aid; patronymic vs "
                "hereditary surname is NOT auto-decided; identities are "
                "grouped by page for review, never merged."),
            "groups_by_page": groups,
            "pages": sorted(pages.values(),
                            key=lambda e: (not e["in_section"], -e["strong"])),
            "household_anchor_leads": anchor_payload,
            "candidates": rows,
        }
        out = OUT_DIR / f"{name}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
        sec_tag = [f"{h.page_id}" for h in top[:5] if h.sec]
        print(f"{name}: {len(hits)} candidates, strong={len(strong)} "
              f"(in-section {sum(1 for h in strong if h.sec)}), "
              f"fuzzy={len(fuzzy)}; top5:")
        for h in top[:5]:
            print(f"   [{h.tier:6s}] {'SEC ' if h.sec else '    '}"
                  f"{h.page_id:9s} l{h.line_number:<4d} "
                  f"«{h.text_raw[:56]}» doc={h.doc_score:.0f}")
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
