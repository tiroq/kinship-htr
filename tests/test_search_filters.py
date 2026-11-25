#!/usr/bin/env python3
"""Self-contained tests: search filters, exact-phrase mode, and evidence
classification over a synthetic index (no archival datasets).

Also guards the §8 required historical-spelling profile coverage:
Шамрай/Шамраев/Шамраи, Головкинъ/Головкин/Головкина, Евфимъ/Ефимъ/Ефим,
Волченокъ/Волченки/Волченок, Михайловка.

Run:  htrbench-python tests/test_search_filters.py   (pytest-compatible)
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._fixtures import build_test_db, make_sidecars  # noqa: E402

FAIL: list[str] = []


def check(cond, msg):
    if cond:
        print(f"PASS {msg}")
    else:
        FAIL.append(msg)
        print(f"FAIL {msg}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tds:
        td = Path(tds)
        sidecar_root = td / "sidecars"
        sidecar_root.mkdir()
        db_path = td / "test.db"
        make_sidecars(sidecar_root)
        build_test_db(db_path, sidecar_root)

        from htr_search.search import Searcher
        s = Searcher(db_path=db_path)

        # --- 1. plain search finds both collections ---------------------
        hits = s.search_lines("Волченокъ")
        check(any(h.page_id == "g999-p000" for h in hits),
              "historical spelling retrieved")
        check(all(h.lead_status == "SECONDARY-LEAD" for h in hits),
              "all machine hits labeled SECONDARY-LEAD")
        check(all(h.evidence_class.startswith("machine") for h in hits),
              f"machine evidence classes: "
              f"{sorted({h.evidence_class for h in hits})}")

        # --- 2. archival filters ----------------------------------------
        hits = s.search_lines("Протасьево", collection="gavo")
        check(hits == [], "collection filter excludes other collections")
        hits = s.search_lines("Протасьево", collection="cgamos-597")
        check(any(h.page_id == "253-p001" for h in hits),
              "collection filter keeps own hits")
        hits = s.search_lines("Волченокъ", fond="И331")
        check(all(h.page_id.startswith("g999") for h in hits) and hits,
              "fond filter")
        hits = s.search_lines("Волченокъ", delo="999")
        check(all(h.page_id == "g999-p000" for h in hits) and hits,
              "delo filter")
        hits = s.search_lines("Демидовъ", page="253-p001")
        check(all(h.page_id == "253-p001" for h in hits) and hits,
              "page filter")
        hits = s.search_lines("Волченокъ", fond="597")
        check(hits == [], "wrong fond yields nothing")

        # --- 3. exact phrase mode ---------------------------------------
        # «дѣти ихъ» normalized -> «дети их»; exact keeps substring matches
        from htr_search.normalize import normalize
        exact = s.search_lines("дѣти ихъ", exact=True)
        check(bool(exact) and all(
            "дети их" in normalize(h.text_raw) for h in exact),
              f"exact mode keeps normalized substring matches: {len(exact)}")
        # exact on a term that appears nowhere as substring (fuzzy would hit);
        # «Протасиво» stays distinct even after ь-fold normalization
        check(s.search_lines("Протасиво", exact=True) == [],
              "exact mode drops fuzzy-only candidates")
        check(s.search_lines("Протасиво"),
              "same query still recalls via fuzzy (control)")

        # --- 4. evidence classification ---------------------------------
        hits = s.search_lines("Протасьево")
        h = next(x for x in hits if x.page_id == "253-p001")
        check(h.match_kind == "exact-raw" and h.evidence_class == "machine-raw",
              f"raw exact label: {h.match_kind}/{h.evidence_class}")
        hits = s.search_lines("дети их")     # matches only after normalization
        g = next((x for x in hits if x.page_id == "g999-p000"
                  and x.line_number == 2), None)
        check(g is not None and g.evidence_class == "machine-normalized",
              f"normalized exact label: "
              f"{g and g.evidence_class}")
        hits = s.search_lines("Протасиво")    # 1-edit typo -> fuzzy
        f = next((x for x in hits if x.page_id == "253-p001"), None)
        check(f is not None and f.match_kind == "fuzzy" and
              f.lead_status == "SECONDARY-LEAD",
              f"fuzzy label: {f and f.match_kind}")

        # human-confirmed review upgrades the label (production write path)
        from tests._fixtures import add_review
        sc253 = json.loads(
            (sidecar_root / "253-p001.htr.json").read_text())
        add_review(db_path, sc253, 1, decision="confirmed",
                   verified_text="село Протасьево приход", notes="human")
        s.close()
        s = Searcher(db_path=db_path)
        hits = s.search_lines("Протасьево")
        h = next(x for x in hits if x.page_id == "253-p001"
                 and x.line_number == 1)
        check(h.evidence_class == "human-verified" and
              h.lead_status == "HUMAN-VERIFIED",
              f"confirmed review upgrades label: {h.evidence_class}")
        h2 = next(x for x in s.search_lines("Демидовъ")
                  if x.page_id == "253-p001" and x.line_number == 2)
        check(h2.lead_status == "SECONDARY-LEAD",
              "unreviewed line stays SECONDARY-LEAD")

        # --- 5. hit dicts carry the labels ------------------------------
        from htr_search.search import hit_to_dict
        d = hit_to_dict(h2)
        check(d["lead_status"] == "SECONDARY-LEAD" and
              d["evidence_class"].startswith("machine"),
              "hit_to_dict exposes evidence fields")

        # --- 6. profile coverage contract (§8 required variants) --------
        from htr_search.profiles import PROFILES, profile_variants
        required = {
            "Шамрай": ["Шамрай", "Шамраев", "Шамраи"],
            "Головкин": ["Головкинъ", "Головкин", "Головкина"],
            "Ефим": ["Евфимъ", "Ефимъ", "Ефим"],
            "Волченки": ["Волченокъ", "Волченки", "Волченок"],
            "Михайловка": ["Михайловка"],
        }
        for prof, variants in required.items():
            check(prof in PROFILES, f"profile exists: {prof}")
            if prof in PROFILES:
                documented = set(PROFILES[prof]["documented"])
                missing = [v for v in variants if v not in documented]
                check(not missing,
                      f"{prof} documented variants {variants} (missing "
                      f"{missing})")
        pv = profile_variants("Шамрай")["all"]
        check(any("шамраев" in v for v in pv) and
              any("шамраи" in v for v in pv),
              "Шамрай profile generates Шамраев/Шамраи retrieval variants")
        pv = profile_variants("Михайловка")["all"]
        from htr_search.normalize import normalize as _norm
        check(_norm("Михайловки") in pv,
              f"Михайловка profile generates inflections "
              f"(normalized {_norm('Михайловки')!r})")
        s.close()

    if FAIL:
        print(f"FAIL ({len(FAIL)}):")
        for f in FAIL:
            print("  -", f)
        return 1
    print("test_search_filters: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
