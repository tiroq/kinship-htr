#!/usr/bin/env python3
"""KINSHIP-HTR-SEARCH-01: locate деревня Волченокъ page ranges per archival case.

Method (fully derived from the OCR index, no new recognition):
  1. Village-section headers are the formula lines «Той же вотчины деревни N
     крестьяне…».  Two facing register pages share one scan, and segmentation
     often splits the header into a formula line («…вотчины деревни») plus a
     name fragment at the same height («волченой» / «Волченокъ»).  A header is
     therefore (formula line) + (name fragment within the same y-band).
  2. A header belongs to Волченокъ when the formula's own text or its adjacent
     name fragment fuzz-matches the village forms attested in this corpus:
     Волченокъ (confirmed) / Волченойъ, валченогъ, во2ченок (likely — same
     village assumed, pending visual verification) / волчьникъ (possible —
     may be a different village).
  3. A section runs from its header (page, y) to the next formula or
     year-block («ведомость …») header, or to the end of the case.

Output: work/discovery/volchenok-sections.json.  All readings are UNVERIFIED
OCR; ranges are search scopes, not archival claims.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/mysterx/dev/kinship-htr-bench")

from rapidfuzz import fuzz  # noqa: E402

from htr_search.normalize import normalize  # noqa: E402
from htr_search.search import Searcher  # noqa: E402

CONFIRMED_FORMS = [normalize(f) for f in
                   ("волченок", "волченка", "волченокъ")]
LIKELY_FORMS = [normalize(f) for f in
                ("волченой", "волченойъ", "валченог", "валченок",
                 "во2ченок")]
POSSIBLE_FORMS = [normalize(f) for f in ("волчьник", "волчник")]

BENCH = Path("/Users/mysterx/dev/kinship-htr-bench")
OUT = BENCH / "work" / "discovery" / "volchenok-sections.json"

def y_frac(row) -> float:
    return row["y_center"] / (row["height"] or 2354)


def name_status(tn: str) -> tuple[str, float] | None:
    """Classify a text against attested village-name forms."""
    for forms, status, thr in ((CONFIRMED_FORMS, "confirmed", 90),
                               (LIKELY_FORMS, "likely", 88),
                               (POSSIBLE_FORMS, "possible", 90)):
        for form in forms:
            if form in tn:
                return status, 100.0
            s = fuzz.partial_ratio(form, tn)
            if s >= thr:
                return status, float(s)
    return None


def main() -> int:
    s = Searcher()
    conn = s.conn

    rows = conn.execute(
        "SELECT l.line_id, l.page_id, l.line_number, l.text_raw, l.y_center, "
        "p.case_short, p.page_number, p.height, p.width "
        "FROM lines l JOIN pages p USING(page_id) "
        "WHERE l.text_norm LIKE '%вот%' OR l.text_norm LIKE '%ведомост%' "
        "ORDER BY p.case_short, p.page_number, l.y_center").fetchall()

    sections = []
    for case in ("253", "255", "256", "258"):
        crows = [r for r in rows if r["case_short"] == case]
        max_page = conn.execute(
            "SELECT MAX(page_number) FROM pages WHERE case_short=?",
            (case,)).fetchone()[0]

        def is_formula(r) -> bool:
            tn = normalize(r["text_raw"])
            return (("деревн" in tn or "дерни" in tn or "дервн" in tn
                     or "дерьни" in tn) and "вот" in tn) or "ведомост" in tn

        formulas = [r for r in crows if is_formula(r)]
        for i, r in enumerate(formulas):
            tn = normalize(r["text_raw"])
            st = name_status(tn)
            frag = None
            if st is None:
                # same-y-band name fragment anywhere on this page
                for n in conn.execute(
                        "SELECT l.line_id, l.text_raw, l.y_center "
                        "FROM lines l WHERE l.page_id=? "
                        "ORDER BY l.y_center", (r["page_id"],)).fetchall():
                    if n["line_id"] == r["line_id"]:
                        continue
                    if abs(n["y_center"] / (r["height"] or 2354)
                           - y_frac(r)) > 0.05:
                        continue
                    st2 = name_status(normalize(n["text_raw"]))
                    if (st2 is not None
                            and 5 <= len(normalize(n["text_raw"])) <= 16):
                        st, frag = st2, n
                        break
            if st is None:
                continue
            status, score = st
            # span end: next formula line strictly after this one
            nxt = next(
                (n for n in formulas[i + 1:]
                 if n["page_number"] > r["page_number"]
                 or (n["page_number"] == r["page_number"]
                     and n["y_center"] > r["y_center"] + 20)),
                None)
            end_page = nxt["page_number"] if nxt else max_page
            end_y = round(y_frac(nxt), 3) if nxt else 1.0
            sections.append({
                "case": case,
                "status": status,
                "name_match_score": score,
                "header_page": r["page_number"], "header_page_id": r["page_id"],
                "header_line_id": r["line_id"],
                "header_y_frac": round(y_frac(r), 3),
                "header_text_raw": r["text_raw"],
                "header_name_fragment": ({"line_id": frag["line_id"],
                                          "text_raw": frag["text_raw"]}
                                         if frag is not None else None),
                "page_range": [r["page_number"], end_page],
                "end_at": {"page": end_page, "y_frac": end_y,
                           "next_header_text":
                               (nxt["text_raw"] if nxt else "(end of case)")},
                "links": {
                    "full_page_image": f"/api/image/{r['page_id']}",
                    "header_crop": f"/api/crop/{r['line_id']}?pad=200",
                },
            })
    s.close()

    by_case = {}
    for s_ in sections:
        by_case.setdefault(s_["case"], []).append(
            {"status": s_["status"], "pages": s_["page_range"]})
    page_sets = {c: sorted({p for s_ in sections if s_["case"] == c
                            for p in range(s_["page_range"][0],
                                           s_["page_range"][1] + 1)})
                 for c in ("253", "255", "256", "258")}
    payload = {
        "village": "деревня Волченки (historical Волченокъ; this corpus also "
                   "shows the clerical form Волченойъ — same village assumed, "
                   "pending visual verification)",
        "method": "village-section headers «Той же вотчины деревни N крестьяне…» "
                  "extracted from OCR (formula line + same-y-band name "
                  "fragment); a section runs to the next village/year header. "
                  "UNVERIFIED OCR: ranges are search scopes, not claims.",
        "status_legend": {
            "confirmed": "header reads a documented form (волченокъ)",
            "likely": "fuzzier read (волченойъ/валченогъ/во2ченок) — same "
                      "village assumed, needs visual confirmation",
            "possible": "weak read (волчьникъ) — may be another village"},
        "sections": sections,
        "sections_by_case": by_case,
        "page_sets": page_sets,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"{len(sections)} sections -> {OUT}")
    for s_ in sections:
        frag = (s_["header_name_fragment"] or {}).get("text_raw", "")
        print(f"  case {s_['case']} [{s_['status']:9s}] pages "
              f"{s_['page_range'][0]:>3}-{s_['page_range'][1]:<3} "
              f"header p{s_['header_page']} y={s_['header_y_frac']} "
              f"«{s_['header_text_raw'][:38]}» + «{frag[:20]}»")
    return 0


if __name__ == "__main__":
    sys.exit(main())
