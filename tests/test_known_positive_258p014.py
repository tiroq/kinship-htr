#!/usr/bin/env python3
"""KINSHIP-HTR-FAMILY-01 Phase A — permanent known-positive regression test.

Ground truth (human-read record, case file, scan 258-p014; transcript NOT
altered to match OCR):

    «Той же вотчины деревни Волченокъ крестьянин Никита Демидов,
     жена его Устинья Мартынова.»

The CRNN OCR of that region reads (line ids 46643-46646):

    Тогми вотчены деревни Волченокъ крестьян-      (header, conf .91)
    Никата умитревъ.                               (conf .91)
    113 жена его устиньй Мартинова-                 (conf .92)
    рат его спиридон                                (conf .99)

Root cause of the original zero-hit Демидов search: recognition corrupted
«Демидовъ» into «умитревъ» (>=5 edits; the surname is unreachable by any
spelling-variant recall), while the retrieval stack additionally (a) let
2-4 char fragments score partial_ratio 100 and (b) pre-trimmed the pool by
layout-inflated total, hiding the household lines that ARE reachable via
the co-names.  Fixed by short-fragment damping in Searcher.search_lines,
no-pretrim pools, and documented household anchors in the Демидов profile.

Run:  htrbench-python tests/test_known_positive_258p014.py
      (also pytest-compatible; expects the local index work/index/htr.db)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCH))

from htr_search.profiles import PROFILES, profile_variants  # noqa: E402
from htr_search.search import Searcher  # noqa: E402

PAGE = "258-p014"
KNOWN = {
    46: "Тогми вотчены деревни Волченокъ крестьян-",   # header
    47: "Никата умитревъ.",                              # head (corrupted)
    48: "113 жена его устиньй Мартинова-",               # wife
    49: "рат его спиридон",                              # brother
}


def _known_lines(conn):
    rows = conn.execute(
        "SELECT line_number, line_id, text_raw FROM lines WHERE page_id=? "
        "AND line_number BETWEEN 46 AND 49 ORDER BY line_number",
        (PAGE,)).fetchall()
    assert len(rows) == 4, f"expected 4 known lines on {PAGE}, got {len(rows)}"
    for r in rows:
        assert r["text_raw"] == KNOWN[r["line_number"]], (
            f"OCR text of {PAGE} l{r['line_number']} changed: "
            f"{r['text_raw']!r} != {KNOWN[r['line_number']]!r} — if the index "
            "was legitimately rebuilt, update KNOWN and the fix re-audit")
    return {r["line_number"]: r["line_id"] for r in rows}


def test_known_lines_indexed():
    s = Searcher()
    try:
        ids = _known_lines(s.conn)
        assert all(ids.values())
    finally:
        s.close()


def test_single_name_searches_retrieve_region():
    """Никита / Устинья / Мартынова / Волченокъ must each surface the
    known-positive region near the top of the corpus-wide ranking."""
    s = Searcher()
    try:
        cases = [  # (query, expected line number, max allowed rank)
            ("Мартынова", 48, 3),
            ("Никита", 47, 300),
            ("Устинья", 48, 50),
            ("Волченок", 46, 5),
        ]
        for query, line_no, max_rank in cases:
            hits = s.search_lines(query, limit=None, with_neighbors=False)
            rank = next((i for i, h in enumerate(hits)
                         if h.page_id == PAGE and h.line_number == line_no),
                        None)
            assert rank is not None, (
                f"«{query}» no longer retrieves {PAGE} l{line_no} — "
                "known-positive regression")
            assert rank < max_rank, (
                f"«{query}»: {PAGE} l{line_no} sank to rank {rank} "
                f"(threshold {max_rank}) — check short-fragment damping")
            # damping invariant: a fragment line must not outrank the
            # known full-length match
            top = hits[0]
            assert len((top.text_raw or "").strip()) >= 4, (
                f"«{query}» top hit is a short fragment: {top.text_raw!r}")
    finally:
        s.close()
def test_demidov_profile_pool_and_anchors():
    """The Демидов profile search must surface the known household through
    its documented household anchors.  The head line itself is provably
    unreachable by surname variants: «никата умитрев» shares ZERO
    trigrams with any generated Демидов corruption (д->у gives «уемидов»,
    whose grams уем/еми/мид/... have no overlap with
    ник/ика/уми/мит/ите/тре/рев), so it can never enter the variant
    candidate pool — the anchor path is the sanctioned retrieval route."""
    s = Searcher()
    try:
        pv = profile_variants("Демидов")
        hits = s.search_lines("Демидов", variants=pv["all"], limit=None,
                              with_neighbors=False)
        assert isinstance(hits, list) and hits, (
            "Демидов profile pool search returned nothing — index or "
            "searcher broken")
        # head line must NOT be claimed as a surname-variant hit
        head = [h for h in hits
                if h.page_id == PAGE and h.line_number == 47]
        assert not head, (
            "l47 entered the Демидов variant pool — either the index was "
            "re-OCR'd or variant generation changed; re-audit the "
            "known-positive diagnosis")

        anchors = PROFILES["Демидов"]["household_anchors_documented"]
        assert set(anchors["co_tokens"]) == {"никита", "устинья",
                                             "мартынова"}
        from name_search import anchor_leads, load_sections
        leads = anchor_leads(s, s.conn, "Демидов", load_sections())
        assert leads is not None
        kp = [L for L in leads["leads"] if L["page_id"] == PAGE]
        assert kp, (
            "Демидов household-anchor leads lost the known-positive "
            "region 258-p014")
        lead = kp[0]
        assert lead["line_span"] == [47, 48], lead["line_span"]
        assert {"никита", "устинья", "мартынова"} <= set(
            lead["matched_anchors"]), lead["matched_anchors"]
        assert lead["in_volchenok_section"], (
            "known-positive region no longer inside a Волченокъ section — "
            "re-run volchenok_sections.py and re-audit")
    finally:
        s.close()


def _main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} known-positive regression "
          "checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
