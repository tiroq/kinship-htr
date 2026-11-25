#!/usr/bin/env python3
"""KINSHIP-HTR-SEARCH-01 Stage D: discovery searches for the three targets.

Runs each search profile (documented spellings + generated inflections +
1-2-edit OCR corruptions; the trigram/fuzzy layer catches the rest) and saves
the ranked top-50 candidates per target to work/discovery/<target>.json,
grouped by archival page, with bbox coordinates, OCR text, original-image
links and a similarity explanation.  Textual similarity (partial_ratio over
variants) is reported separately from OCR confidence (which only ever
contributes a capped <=3-point hint in the total; see htr_search/search.py).

Two-tier ranking (the raw partial_ratio lets junk short fragments like "Ата"
win against corruption variants):
  strong — the line's normalized text CONTAINS a documented spelling or a
           generated inflection (real-word evidence), or scores >= 90 against
           one while being long enough to carry the target name;
  fuzzy  — everything else above min_text_score (corruption-variant matches,
           kept for recall, never ranked above strong).

A hit is a LEAD, not a claim: OCR text is unverified and riddled with
recognition error; nothing here is genealogical evidence until a human
confirms it in the review UI.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/mysterx/dev/kinship-htr-bench")

from rapidfuzz import fuzz  # noqa: E402

from htr_search.normalize import normalize  # noqa: E402
from htr_search.profiles import PROFILES, profile_variants  # noqa: E402
from htr_search.search import Searcher  # noqa: E402

BENCH = Path("/Users/mysterx/dev/kinship-htr-bench")
OUT_DIR = BENCH / "work" / "discovery"
TOP_N = 50
FUZZY_MIN_LEN = 6   # a line shorter than this cannot carry a target name


def explain(tier, doc_score, gen_score, h) -> str:
    parts = [f"[{tier}] documented-variant similarity {doc_score:.0f}/100"]
    if gen_score > doc_score:
        parts.append(f"generated-variant similarity {gen_score:.0f}/100 "
                     "(corruption/inflection recall — a lead, not a spelling)")
    if h.context_score:
        parts.append(f"neighbouring-line similarity {h.context_score:.0f}/100")
    if h.layout_score:
        parts.append(f"layout/source bonus {h.layout_score:.0f} pts")
    if h.confidence is not None:
        parts.append(f"OCR confidence {h.confidence:.2f} (hint only, capped "
                     "in total_score)")
    return "; ".join(parts)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    s = Searcher()
    summary = {}
    for name in PROFILES:
        pv = profile_variants(name)
        canon = min(len(normalize(name)), 8)
        t0 = time.time()
        hits = s.search_lines(name, variants=pv["all"], limit=5000,
                              with_neighbors=True)
        run_id = s.log_run(name, name, hits[:TOP_N])

        # ---- two-tier classification on top of the searcher's recall net ----
        doc_v = pv["documented"]
        inf_v = [normalize(v) for v in
                 PROFILES[name]["inflections_generated"]]
        strong_v = doc_v + inf_v
        gen_v = pv["generated"]
        strong, fuzzy = [], []
        for h in hits:
            tn = normalize(h.text_raw)
            doc_score = max((fuzz.partial_ratio(v, tn) for v in doc_v),
                            default=0.0)
            gen_score = max((fuzz.partial_ratio(v, tn) for v in gen_v),
                            default=0.0)
            contained = any(v in tn for v in strong_v)
            if contained or (doc_score >= 90 and len(tn) >= FUZZY_MIN_LEN):
                h.tier, h.doc_score, h.gen_score = "strong", doc_score, gen_score
                strong.append(h)
            else:
                h.tier, h.doc_score, h.gen_score = "fuzzy", doc_score, gen_score
                fuzzy.append(h)
        strong.sort(key=lambda h: (-h.doc_score, -h.total))
        fuzzy.sort(key=lambda h: -h.total)
        top = (strong + fuzzy)[:TOP_N]

        pages: dict[str, dict] = {}
        rows = []
        for rank, h in enumerate(top, 1):
            rows.append({
                "rank": rank,
                "tier": h.tier,
                "line_id": h.line_id,
                "page_id": h.page_id,
                "line_number": h.line_number,
                "source": h.source,
                "bbox": h.bbox,
                "text_raw": h.text_raw,
                "ocr_confidence": h.confidence,
                "text_similarity_documented": round(h.doc_score, 1),
                "text_similarity_generated": round(h.gen_score, 1),
                "context_similarity": round(h.context_score, 1),
                "layout_points": round(h.layout_score, 1),
                "total_score": round(h.total, 1),
                "matched_variant": h.matched_variant,
                "variant_class": ("documented" if h.matched_variant
                                  in doc_v else "generated"),
                "similarity_explanation": explain(h.tier, h.doc_score,
                                                   h.gen_score, h),
                "neighboring_lines": [
                    {"line_id": nb["line_id"], "line_number": nb["line_number"],
                     "text_raw": nb["text_raw"]} for nb in h.neighbors],
                "review": (dict(h.review) if h.review else None),
                "links": {
                    "full_page_image": f"/api/image/{h.page_id}",
                    "crop_around_bbox": f"/api/crop/{h.line_id}?pad=120",
                    "line_api": f"/api/line/{h.line_id}",
                },
            })
            e = pages.setdefault(h.page_id, {"page_id": h.page_id,
                                             "candidate_count": 0,
                                             "strong_count": 0,
                                             "best_total": 0.0})
            e["candidate_count"] += 1
            e["strong_count"] += (h.tier == "strong")
            e["best_total"] = max(e["best_total"], round(h.total, 1))

        payload = {
            "target": name,
            "profile_type": PROFILES[name]["type"],
            "canonical": PROFILES[name]["canonical"],
            "documented_spellings": PROFILES[name]["documented"],
            "documented_note": PROFILES[name]["documented_note"],
            "documented_variants_normalized": doc_v,
            "generated_inflections": inf_v,
            "generated_corruptions_used": len(gen_v),
            "generated_sample": gen_v[:15],
            "candidates_above_min_score": len(hits),
            "strong_candidates": len(strong),
            "fuzzy_candidates": len(fuzzy),
            "saved_top_n": len(rows),
            "search_run_id": run_id,
            "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "elapsed_s": round(time.time() - t0, 1),
            "scoring_note": (
                "text_similarity_documented = best RapidFuzz partial_ratio "
                "against documented spellings; text_similarity_generated "
                "against generated inflections/corruptions (recall only, "
                "never a spelling claim). OCR confidence is reported "
                "separately and contributes at most a 3-point capped hint "
                "inside total_score. tier=strong means the line's normalized "
                "text contains a real spelling/inflection (or >=90 similarity "
                "on a line long enough to carry the name); tier=fuzzy is "
                "corruption-variant recall. OCR text is UNVERIFIED: every "
                "candidate is a lead, not a reading."),
            "pages": sorted(pages.values(),
                            key=lambda e: (-e["strong_count"],
                                           -e["best_total"])),
            "candidates": rows,
        }
        out = OUT_DIR / f"{name}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
        summary[name] = (len(hits), len(strong), len(fuzzy),
                         [(h.tier, h.page_id, h.text_raw[:60],
                           round(h.doc_score, 0)) for h in top[:5]])
        print(f"{name}: {len(hits)} candidates (strong={len(strong)} "
              f"fuzzy={len(fuzzy)}), top {len(rows)} over {len(pages)} pages "
              f"-> {out}")
        for tier, pid, text, ds in summary[name][3]:
            print(f"   [{tier:6s}] doc={ds:5.0f}  {pid:9s}  {text}")
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
