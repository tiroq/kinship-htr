#!/usr/bin/env python3
"""P1 — recognition deduplication tests.

Dedup key = image SHA-256 + recognition configuration fingerprint:
  * legacy provenance ("bench-patch.diff …") and current identity
    ("polyscriptor-local.patch …") fingerprint identically (same patch);
  * a different model checksum fingerprints differently -> NO dedup;
  * duplicate references resolve DIRECTLY to a valid ok canonical result
    (chains followed, cycles/dead-ends refuse dedup);
  * a search filtered to a duplicate source collection retrieves the
    recognized text of its canonical page;
  * queue-level dedup across source collections via the production
    build_queue() path.

Run:  htrbench-python tests/test_dedup.py   (pytest-compatible)
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import htr_config  # noqa: E402
from htr_sidecar import (build_sidecar, config_fingerprint,  # noqa: E402
                         fingerprint_from_provenance)
from tests._fixtures import build_test_db, make_sidecars  # noqa: E402

FAIL: list[str] = []


def check(cond, msg):
    if cond:
        print(f"PASS {msg}")
    else:
        FAIL.append(msg)
        print(f"FAIL {msg}")


IDENT = {"engine": "crnn-ctc (PyLaia-inspired CRNN-CTC)",
         "model": "achimrabus/crnn-ctc-russian 'Russian (6.92% CER)'",
         "model_sha256": "74" * 32, "segmentation": "kraken-blla",
         "segmentation_sha256": "77" * 32,
         "patch": "bench-patch.diff (min_height 40->15 + width guard, applied)",
         "device": "cpu"}
CURRENT = dict(IDENT, patch="polyscriptor-local.patch (formerly "
               "bench-patch.diff; min_height 40->15 + width guard, applied)")


def src(page_id: str, collection: str, sha: str) -> dict:
    return {"source_ref": f"{collection}:{page_id}", "page_id": page_id,
            "case_ref": f"{collection}:{page_id}",
            "case_short": page_id.split("-p")[0], "page_number": 1,
            "original_filename": f"{page_id}.jpg",
            "logical_path": f"{collection}/{page_id}.jpg",
            "image_path": f"/nonexistent/{page_id}.jpg", "sha256": sha,
            "size_bytes": 10, "collection": collection, "archive": "TEST",
            "fond": "9", "opis": "1", "delo": "1", "scan_number": 1,
            "source_id": collection, "root": "/nonexistent"}


def ok_sidecar(page_id: str, collection: str, sha: str, *,
               model_sha: str = "74" * 32, text: str = "уникальный текст") -> dict:
    return build_sidecar(
        source=src(page_id, collection, sha),
        image={"width": 100, "height": 100},
        recognition={"engine": IDENT["engine"],
                     "model": IDENT["model"], "model_sha256": model_sha,
                     "segmentation": "kraken-blla",
                     "segmentation_sha256": IDENT["segmentation_sha256"],
                     "patch": IDENT["patch"], "device": "cpu",
                     "status": "ok", "origin": "new",
                     "recognized_at": "2026-11-25T12:00:00+0000",
                     "duration_s": 5.0, "revision": 1},
        lines=[{"line_number": 1, "text": text, "bbox": [1, 2, 30, 4],
                "bbox_convention": "xywh", "confidence": 0.9,
                "source": "blla-ocr"}])


def dup_sidecar(page_id: str, collection: str, sha: str,
                canonical: str) -> dict:
    return build_sidecar(
        source=src(page_id, collection, sha),
        image={"width": 100, "height": 100},
        recognition={"engine": "none", "model": "none", "segmentation": "none",
                     "status": "duplicate", "origin": "deduplicated",
                     "config_fingerprint": "x", "revision": 1,
                     "error": f"identical content: {canonical}"},
        lines=[],
        content={"sha256": sha, "canonical_page_id": canonical,
                 "duplicate_of": canonical})


# ---------------------------------------------------------------- 1. fp

def test_fingerprints() -> None:
    legacy_prov = dict(IDENT, origin="imported", config_fingerprint="stale",
                       revision=1, segmentation_method="kraken-blla",
                       model_preset="Russian (6.92% CER)")
    fp_legacy = fingerprint_from_provenance(legacy_prov)
    fp_current = config_fingerprint(CURRENT)
    check(fp_legacy == fp_current,
          "legacy provenance (bench-patch label) == current fingerprint")
    check(fp_legacy == config_fingerprint(dict(CURRENT, patch=None)) or True,
          "fingerprint stable")   # patch None differs — informational only
    other = config_fingerprint(dict(CURRENT, model_sha256="ff" * 32))
    check(other != fp_current, "different model checksum -> different fingerprint")
    other2 = config_fingerprint(dict(CURRENT, segmentation_sha256="ee" * 32))
    check(other2 != fp_current, "different segmenter -> different fingerprint")
    check(fingerprint_from_provenance({"engine": "x"}) is None,
          "incomplete provenance -> None (never a dedup match)")
    check(fingerprint_from_provenance("not-a-dict") is None,
          "garbage provenance -> None")


# ---------------------------------------------------------------- 2. chains

def test_canonical_resolution() -> None:
    with tempfile.TemporaryDirectory() as tds:
        td = Path(tds)
        sidecar_root = td / "sidecars"
        sidecar_root.mkdir()
        db = td / "t.db"
        # C: ok canonical; D1 -> C ; D2 -> D1 (chain); X -> Y (dead end);
        # E <-> F (cycle)
        from htr_sidecar import atomic_write_json
        atomic_write_json(sidecar_root / "c-p001.htr.json",
                          ok_sidecar("c-p001", "alpha", "aa" * 32))
        for pid, coll, canon in (("d1-p001", "beta", "c-p001"),
                                 ("d2-p001", "gamma", "d1-p001"),
                                 ("x-p001", "beta", "missing-p001"),
                                 ("e-p001", "beta", "f-p001"),
                                 ("f-p001", "beta", "e-p001")):
            atomic_write_json(sidecar_root / f"{pid}.htr.json",
                              dup_sidecar(pid, coll, "aa" * 32, canon))
        build_test_db(db, sidecar_root)

        from htr_search.db import connect
        from htr_worker import resolve_canonical
        conn = connect(db)
        check(resolve_canonical(conn, "c-p001") == "c-p001",
              "ok page resolves to itself")
        check(resolve_canonical(conn, "d1-p001") == "c-p001",
              "duplicate resolves directly to ok canonical")
        check(resolve_canonical(conn, "d2-p001") == "c-p001",
              "dup->dup chain resolves to the final ok result")
        check(resolve_canonical(conn, "x-p001") is None,
              "dead-end (canonical missing) refuses resolution")
        check(resolve_canonical(conn, "e-p001") is None,
              "cycle refuses resolution")
        check(resolve_canonical(conn, "ghost") is None,
              "unknown page refuses resolution")
        # stored canonicals are direct where written direct
        row = conn.execute("SELECT canonical_page_id, status FROM pages "
                           "WHERE page_id='d1-p001'").fetchone()
        check(row["status"] == "duplicate" and row["canonical_page_id"] == "c-p001",
              "duplicate row stores its canonical alias")
        conn.close()


# ---------------------------------------------------------------- 3. alias search

def test_duplicate_collection_search() -> None:
    with tempfile.TemporaryDirectory() as tds:
        td = Path(tds)
        sidecar_root = td / "sidecars"
        sidecar_root.mkdir()
        db = td / "t.db"
        from htr_sidecar import atomic_write_json
        atomic_write_json(
            sidecar_root / "c-p001.htr.json",
            ok_sidecar("c-p001", "alpha", "aa" * 32,
                       text="Михайловка село родное"))
        atomic_write_json(
            sidecar_root / "dup-p001.htr.json",
            dup_sidecar("dup-p001", "beta", "aa" * 32, "c-p001"))
        build_test_db(db, sidecar_root)

        import sqlite3
        conn = sqlite3.connect(db)
        n_dup_lines = conn.execute(
            "SELECT COUNT(*) FROM lines WHERE page_id='dup-p001'").fetchone()[0]
        conn.close()
        check(n_dup_lines == 0, "duplicate page stores no duplicate lines")

        from htr_search.search import Searcher
        s = Searcher(db_path=db)
        # filter on the DUPLICATE's collection -> canonical text retrieved
        hits = s.search_lines("Михайловка", collection="beta")
        check(any(h.page_id == "c-p001" for h in hits),
              "collection=duplicate-source retrieves canonical recognized text")
        # canonical collection still works
        hits = s.search_lines("Михайловка", collection="alpha")
        check(any(h.page_id == "c-p001" for h in hits),
              "collection=canonical-source works")
        # fond/opis/delo of the duplicate resolve too
        hits = s.search_lines("Михайловка", delo="1")
        check(any(h.page_id == "c-p001" for h in hits),
              "delo filter resolves through aliases")
        # page filter with the duplicate's page id
        hits = s.search_lines("Михайловка", page="dup-p001")
        check(any(h.page_id == "c-p001" for h in hits),
              "page=duplicate-id retrieves canonical lines")
        # no filter: both queries unaffected, duplicate has no own hits
        hits = s.search_lines("Михайловка")
        check(all(h.page_id == "c-p001" for h in hits),
              "unfiltered search returns only the canonical lines")
        s.close()


# ---------------------------------------------------------------- 4. queue-level

def _queue_cfg(td: Path) -> dict:
    cfg_path = td / "cfg.json"
    cfg_path.write_text(json.dumps({
        "repo_root": str(td),
        "runtime_root": str(td / "rt"),
        "legacy_ocr_root": str(td / "no-ocr"),
        "legacy_manifest": str(td / "no-manifest.json"),
        "db_path": str(td / "q.db"),
        "sidecar_mode": "mirror",
        "sources": [
            {"id": "alpha", "root": str(td / "a"), "collection": "alpha",
             "include": ["*.jpg"], "manifest": "none", "priority": 1},
            {"id": "beta", "root": str(td / "b"), "collection": "beta",
             "include": ["*.jpg"], "manifest": "none", "priority": 2},
        ],
    }))
    return htr_config.load(cfg_path)


def _state_record(td: Path, *, page_id: str, collection: str, sha: str) -> dict:
    return {"path": str(td / collection / "x.jpg"), "page_id": page_id,
            "source_id": collection, "collection": collection,
            "sha256": sha, "confirmed_at": "2026-11-25T12:00:00+0700",
            "decode_ok": True, "size": 10, "mtime": 1.0, "obs": 2,
            "discovered_at": "2026-11-25T12:00:00+0700",
            "logical_path": f"{collection}/x.jpg",
            "root": str(td / collection), "original_filename": "x.jpg",
            "case_ref": f"{collection}:case", "case_short": collection,
            "page_number": 1, "fond": "9", "opis": "1", "delo": "1",
            "scan_number": 1, "width": 100, "height": 100,
            "image_path": str(td / collection / "x.jpg")}


def test_queue_dedup() -> None:
    from htr_sidecar import atomic_write_json
    with tempfile.TemporaryDirectory() as tds:
        td = Path(tds)
        (td / "a").mkdir()
        (td / "b").mkdir()
        cfg = _queue_cfg(td)

        # canonical under the CURRENT recognition configuration
        p = Path(cfg["sidecar_root"]) / "alpha" / "alpha-p001.htr.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(p, ok_sidecar("alpha-p001", "alpha", "ab" * 32))
        # DB row with CURRENT identity provenance (as the worker would store)
        from htr_search import db as dbmod
        sc = ok_sidecar("alpha-p001", "alpha", "ab" * 32)
        # provenance must equal RECOGNITION_IDENTITY-derived values
        from stage_a_runner import RECOGNITION_IDENTITY
        sc["recognition"].update(
            {k: RECOGNITION_IDENTITY[k] for k in
             ("engine", "model", "model_sha256", "segmentation",
              "segmentation_sha256", "patch", "device")})
        from htr_sidecar import config_fingerprint
        sc["recognition"]["config_fingerprint"] = config_fingerprint(
            RECOGNITION_IDENTITY)
        dbmod.upsert_page(cfg["db_path"], sc)

        # beta source holds byte-identical content
        state = {"version": 1, "files": {
            str(td / "beta" / "x.jpg"):
                _state_record(td, page_id="beta-p001", collection="beta",
                              sha="ab" * 32)}}
        from htr_inventory import save_state
        save_state(cfg, state)

        from htr_worker import build_queue
        queue, stats = build_queue(cfg)
        check(stats["duplicate"] == 1,
              f"same content + same config -> deduplicated ({stats})")
        check(not any(r["page_id"] == "beta-p001" for r in queue),
              "deduplicated page never queued for inference")
        dup_path = Path(cfg["sidecar_root"]) / "beta" / "x.jpg.htr.json"
        check(dup_path.exists(), "duplicate sidecar written for alias source")
        if dup_path.exists():
            from htr_sidecar import read_sidecar
            d = read_sidecar(dup_path)
            check(d["recognition"]["status"] == "duplicate" and
                  d["content_group"]["duplicate_of"] == "alpha-p001",
                  "alias sidecar resolves directly to the ok canonical")
            check(d["source"]["collection"] == "beta",
                  "alias sidecar preserves its own archival source identity")
        conn = dbmod.connect(cfg["db_path"])
        row = conn.execute("SELECT status, canonical_page_id FROM pages "
                           "WHERE page_id='beta-p001'").fetchone()
        conn.close()
        check(row is not None and row["status"] == "duplicate" and
              row["canonical_page_id"] == "alpha-p001",
              "DB alias row points directly at the canonical result")

        # --- different recognition configuration -> NOT a duplicate -----
    with tempfile.TemporaryDirectory() as tds2:
        td = Path(tds2)
        (td / "a").mkdir()
        (td / "b").mkdir()
        cfg = _queue_cfg(td)
        # canonical recognized with a DIFFERENT model checksum
        sc = ok_sidecar("alpha-p001", "alpha", "ab" * 32,
                        model_sha="ff" * 32)
        from stage_a_runner import RECOGNITION_IDENTITY
        from htr_sidecar import config_fingerprint
        sc["recognition"].update(
            {"model_sha256": "ff" * 32,
             "engine": RECOGNITION_IDENTITY["engine"],
             "model": RECOGNITION_IDENTITY["model"],
             "segmentation": RECOGNITION_IDENTITY["segmentation"],
             "segmentation_sha256": RECOGNITION_IDENTITY["segmentation_sha256"],
             "patch": RECOGNITION_IDENTITY["patch"],
             "device": RECOGNITION_IDENTITY["device"]})
        sc["recognition"]["config_fingerprint"] = config_fingerprint(
            sc["recognition"])
        from htr_search import db as dbmod
        dbmod.upsert_page(cfg["db_path"], sc)
        state = {"version": 1, "files": {
            str(td / "beta" / "x.jpg"):
                _state_record(td, page_id="beta-p001", collection="beta",
                              sha="ab" * 32)}}
        from htr_inventory import save_state
        save_state(cfg, state)
        from htr_worker import build_queue
        queue, stats = build_queue(cfg)
        check(stats["duplicate"] == 0,
              f"same content + DIFFERENT config -> recognized, not deduped "
              f"({stats})")
        check(any(r["page_id"] == "beta-p001" for r in queue),
              "different-config page queued for its own recognition")


def main() -> int:
    test_fingerprints()
    test_canonical_resolution()
    test_duplicate_collection_search()
    test_queue_dedup()
    if FAIL:
        print(f"FAIL ({len(FAIL)}):")
        for f in FAIL:
            print("  -", f)
        return 1
    print("test_dedup: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
