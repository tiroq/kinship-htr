#!/usr/bin/env python3
"""CLI search over the local HTR index.

Usage (from /Users/mysterx/dev/kinship-htr-bench, htrbench env python):
  python -m htr_search.cli Волченки --profile Волченки --limit 50
  python -m htr_search.cli "протасьево" --case 255 --json out.json
  python -m htr_search.cli --multiline $'село волченки\n1834 год'

Every hit is a LEAD for human verification, not a conclusion; absence of
hits is not evidence of absence (OCR recall is imperfect by design).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from htr_search.db import DB_PATH  # noqa: E402
from htr_search.profiles import PROFILES, profile_variants  # noqa: E402
from htr_search.search import Hit, Searcher, hit_to_dict  # noqa: E402


def group_by_page(hits: list[Hit]) -> list[dict]:
    pages: dict[str, dict] = {}
    for h in hits:
        e = pages.setdefault(h.page_id, {"page_id": h.page_id, "hits": [],
                                         "best": 0.0})
        e["hits"].append(h)
        e["best"] = max(e["best"], h.total)
    return sorted(pages.values(), key=lambda e: -e["best"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("--profile", choices=list(PROFILES), default=None)
    ap.add_argument("--case", default=None, help="case short id: 253/255/256/258")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--min-score", type=float, default=None)
    ap.add_argument("--multiline", action="store_true",
                    help="treat query as multi-line (newline-separated)")
    ap.add_argument("--json", default=None, help="write results to JSON file")
    ap.add_argument("--log-run", action="store_true",
                    help="persist run+results in search_runs/search_results")
    ns = ap.parse_args()

    if not Path(DB_PATH).exists():
        print(f"index missing: {DB_PATH} — run htr_search.db first",
              file=sys.stderr)
        return 2

    t0 = time.time()
    s = Searcher()
    if ns.min_score is not None:
        s.w["min_text_score"] = ns.min_score
    variants = None
    profile_name = None
    if ns.profile:
        pv = profile_variants(ns.profile)
        variants = pv["all"]
        profile_name = ns.profile

    if ns.multiline:
        result = s.search_multiline(ns.query, case=ns.case, limit=ns.limit)
        payload = {"query": ns.query, "profile": profile_name, "mode": "multiline",
                   "pages": result["pages"],
                   "note": "page score = sum of best hit per query line"}
    else:
        hits = s.search_lines(ns.query, variants=variants, case=ns.case,
                              limit=ns.limit)
        payload = {"query": ns.query, "profile": profile_name,
                   "variants_used": len(variants) if variants else 1,
                   "hits": [hit_to_dict(h) for h in hits],
                   "pages": [{"page_id": p["page_id"], "best": round(p["best"], 1),
                              "line_ids": [h.line_id for h in p["hits"]]}
                             for p in group_by_page(hits)]}

    dt = time.time() - t0
    payload["elapsed_s"] = round(dt, 2)
    payload["note"] = ("leads only — human visual verification required; "
                       "no hits != absence")

    if ns.log_run and not ns.multiline:
        s.log_run(ns.query, profile_name, hits)

    if ns.json:
        Path(ns.json).write_text(json.dumps(payload, ensure_ascii=False, indent=1))
        print(f"results written -> {ns.json}")

    # human-readable output
    if ns.multiline:
        for p in payload["pages"]:
            print(f"\n== {p['page_id']}  score={p['score']}")
            for h in p["hits"]:
                print(f"  [{h['line_number']:>4}] {h['text_raw'][:70]!r} "
                      f"score={h['score']}")
    else:
        for h in payload["hits"]:
            rev = ""
            if h["review"]:
                rev = f" review={h['review']['decision']}"
            print(f"{h['score']:6.1f} {h['page_id']} l{h['line_number']:>4} "
                  f"[{h['source']}] {h['text_raw'][:60]!r} "
                  f"text={h['components']['text']} ctx={h['components']['context']} "
                  f"lay={h['components']['layout']} v={h['components']['matched_variant']!r}{rev}")
    n = len(payload.get("hits", payload.get("pages", [])))
    print(f"\n{n} result(s) in {dt:.1f}s  db={DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
