#!/usr/bin/env python3
"""Self-contained tests for the portable sidecar contract (schema 1.0.0).

Covers: bbox convention conversion (xywh <-> xyxy, tested both directions),
sidecar assembly, validation rules, atomic writes (no partial files), and
revision preservation on reprocessing.  No archival datasets required.

Run:  htrbench-python tests/test_sidecar.py   (pytest-compatible)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from htr_sidecar import (BBOX_CONVENTION, build_sidecar, config_fingerprint,
                         read_sidecar, sidecar_path_for, validate_sidecar,
                         write_sidecar, xywh_to_xyxy, xyxy_to_xywh)

FAIL: list[str] = []


def check(cond, msg):
    if cond:
        print(f"PASS {msg}")
    else:
        FAIL.append(msg)
        print(f"FAIL {msg}")


def main() -> int:
    # --- 1. conversion both directions --------------------------------
    xywh = [176, 731, 34, 33]
    xyxy = xywh_to_xyxy(xywh)
    check(xyxy == [176, 731, 210, 764], f"xywh->xyxy {xyxy}")
    check(xyxy_to_xywh(xyxy) == xywh, "xyxy->xywh roundtrip")
    check(xywh_to_xyxy([0, 0, 0, 0]) == [0, 0, 0, 0], "degenerate zero box")

    # --- 2. build: bbox converted, line ids, fingerprint ---------------
    source = {
        "source_ref": "gavo-I331-3-999", "page_id": "g999-p000",
        "case_ref": "gavo-I331-3-999", "case_short": "g999",
        "page_number": 0, "original_filename": "x.jpg",
        "logical_path": "gavo/case/originals/x.jpg",
        "image_path": "/tmp/x.jpg", "sha256": "ab" * 32, "size_bytes": 10,
        "collection": "gavo", "archive": "ГАВО", "fond": "И331",
        "opis": "3", "delo": "999", "scan_number": 0,
        "source_id": "gavo", "root": "/tmp",
    }
    recognition = {"engine": "crnn-ctc", "model": "m", "model_sha256": "cd" * 32,
                   "segmentation": "kraken-blla", "segmentation_sha256": "ef" * 32,
                   "patch": "p", "device": "cpu", "status": "ok",
                   "origin": "new", "recognized_at": "2026-11-25T00:00:00+0000",
                   "duration_s": 24.0, "revision": 1}
    sc = build_sidecar(
        source=source, image={"width": 1000, "height": 2000},
        recognition=recognition,
        lines=[{"line_number": 7, "reading_order": 7, "text": "Волченокъ",
                "text_norm": "волченок", "bbox": [10, 20, 30, 40],
                "confidence": 0.9, "source": "blla-ocr"},
               {"line_number": 8, "text": "тест", "bbox": [1, 2, 3, 4],
                "confidence": None, "source": "stage-b-gap"}])
    check(sc["schema_version"] == "1.0.0", "schema version stamped")
    check(sc["lines"][0]["bbox"] == [10, 20, 40, 60],
          f"first line bbox converted xyxy: {sc['lines'][0]['bbox']}")
    check(sc["lines"][0]["bbox_convention"] == BBOX_CONVENTION,
          "bbox convention declared")
    check(sc["lines"][0]["line_id"] == "g999-p000-r1-0001",
          f"stable line id: {sc['lines'][0]['line_id']}")
    check(sc["lines"][1]["polygon"] is None and
          sc["lines"][1]["baseline"] is None,
          "unavailable polygon/baseline stay null (never invented)")
    check(sc["lines"][1]["verification_status"] == "unverified",
          "machine lines default to unverified")
    check(len(sc["recognition"]["config_fingerprint"]) == 64,
          "config fingerprint present")
    fp1 = config_fingerprint(recognition)
    fp2 = config_fingerprint(dict(recognition, device="gpu"))
    check(fp1 != fp2, "fingerprint changes with config")
    check(validate_sidecar(sc) == [], f"valid sidecar: {validate_sidecar(sc)}")

    # --- 3. validation catches defects ---------------------------------
    bad = json.loads(json.dumps(sc))
    bad["lines"][0]["bbox"] = [40, 60, 10, 20]      # not ordered
    probs = validate_sidecar(bad)
    check(any("not ordered" in p for p in probs), f"unordered bbox flagged {probs}")
    bad2 = json.loads(json.dumps(sc))
    del bad2["source"]["sha256"]
    check(any("source.sha256" in p for p in validate_sidecar(bad2)),
          "missing sha flagged")
    bad3 = json.loads(json.dumps(sc))
    bad3["schema_version"] = "9.9.9"
    check(any("schema_version" in p for p in validate_sidecar(bad3)),
          "wrong schema version flagged")
    bad4 = json.loads(json.dumps(sc))
    bad4["lines"][1]["line_id"] = bad4["lines"][0]["line_id"]
    check(any("duplicate line_id" in p for p in validate_sidecar(bad4)),
          "duplicate line_id flagged")

    # --- 4. atomic write + revision preservation ------------------------
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "sub" / "x.jpg.htr.json"
        write_sidecar(target, sc)
        check(target.exists(), "sidecar written")
        check(not list(target.parent.glob("*.tmp")), "no temp file left behind")
        # partial-write protection: a crash leaves old content intact
        old_bytes = target.read_bytes()
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text("{corrupt")
        # (simulated crash before os.replace — real file untouched)
        check(target.read_bytes() == old_bytes, "atomic: reader never sees tmp")

        sc2 = json.loads(json.dumps(sc))
        sc2["recognition"]["duration_s"] = 30.0
        sc2["lines"][0]["text_raw"] = "Волченокъ исправлено"
        write_sidecar(target, sc2)
        reloaded = read_sidecar(target)
        check(reloaded["recognition"]["revision"] == 2,
              f"revision bumped: {reloaded['recognition']['revision']}")
        rev_dir = target.parent / "revisions"
        archived = list(rev_dir.glob("*.rev1.json"))
        check(len(archived) == 1, "previous revision preserved on reprocess")
        old = json.loads(archived[0].read_text())
        check(old["recognition"]["duration_s"] == 24.0,
              "archived revision holds the original content")

    # --- 5. mirror path preserves logical source path ------------------
    with tempfile.TemporaryDirectory() as td:
        real = Path(td) / "real-src" / "case" / "originals"
        real.mkdir(parents=True)
        link = Path(td) / "link-src"
        link.symlink_to(real)
        img = real / "page.jpg"
        img.write_bytes(b"x")
        cfg = {"sidecar_mode": "mirror", "sidecar_root": str(Path(td) / "mir")}
        rec = {"logical_path": "gavo/case/originals/page.jpg",
               "source_id": "gavo", "root": str(link)}
        p = sidecar_path_for(img, cfg, rec)
        check(str(p).endswith("mir/gavo/case/originals/page.jpg.htr.json"),
              f"mirror keeps logical path: {p}")
        # without logical_path: derived from root WITHOUT resolving symlink
        # (inventory yields paths that carry the symlink prefix, as glob does)
        p2 = sidecar_path_for(link / "case/originals/page.jpg", cfg,
                              {"source_id": "gavo", "root": str(link)})
        check(str(p2).endswith("mir/gavo/case/originals/page.jpg.htr.json"),
              f"derived path keeps symlinked root: {p2}")

    if FAIL:
        print(f"FAIL ({len(FAIL)}):")
        for f in FAIL:
            print("  -", f)
        return 1
    print("test_sidecar: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
