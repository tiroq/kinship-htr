#!/usr/bin/env python3
"""Self-contained tests: legacy recognition artifacts -> portable sidecars.

Builds a synthetic legacy page directory (meta.json + transcriptions +
stage_b.json, the exact layout of work/index/ocr/<pid>/) and verifies the
import: bbox conversion, line sources, normalized text, origin/import
validation flags, idempotent skip, and honest flagging of a missing
original.  No archival datasets needed.

Run:  htrbench-python tests/test_legacy_import.py   (pytest-compatible)
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import htr_config
from htr_import import legacy_to_sidecars, page_dir_to_sidecar  # noqa: E402
from htr_sidecar import read_sidecar, validate_sidecar  # noqa: E402

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
        img = td / "originals" / "000900.jpg"
        img.parent.mkdir(parents=True)
        img.write_bytes(b"\xff\xd8\xff\xe0fakejpeg-bytes\xff\xd9")
        sha = hashlib.sha256(img.read_bytes()).hexdigest()

        page = td / "ocr" / "253-p900"
        (page / "transcriptions").mkdir(parents=True)
        (page / "meta.json").write_text(json.dumps({
            "page_id": "253-p900", "case_id": "0597-0001-000253",
            "page_number": 900, "image_path": str(img),
            "image_sha256": sha,
            "engine": "crnn-ctc (PyLaia-inspired CRNN-CTC)",
            "model": "achimrabus/crnn-ctc-russian 'Russian (6.92% CER)'",
            "model_sha256": "74" * 32, "segmentation": "kraken-blla",
            "blla_sha256": "77" * 32,
            "patch": "bench-patch.diff", "device": "cpu",
            "status": "ok", "line_count": 2, "avg_confidence": 0.8,
            "elapsed_s": 22.5, "completed_at": "2026-11-22T10:00:00+0700",
        }, ensure_ascii=False))
        (page / "transcriptions" / "253-p900.json").write_text(json.dumps({
            "image": "253-p900.jpg", "engine": "crnn-ctc",
            "lines": [
                {"line_number": 1, "text": "деревни Волченокъ",
                 "confidence": 0.9, "bbox": [10, 20, 100, 30]},
                {"line_number": 2, "text": "дѣти ихъ",
                 "confidence": 0.7, "bbox": [10, 60, 100, 30]},
            ],
        }, ensure_ascii=False))
        (page / "stage_b.json").write_text(json.dumps({
            "provenance": "stage-b-gap-recovery (v1)", "bands_examined": 3,
            "lines": [{"line_number": 1, "text": "строка из разрывы",
                       "confidence": 0.4, "bbox": [10, 500, 80, 25]}],
        }, ensure_ascii=False))

        # a page whose original vanished (honest flag expected)
        gone = td / "ocr" / "253-p901"
        (gone / "transcriptions").mkdir(parents=True)
        (gone / "meta.json").write_text(json.dumps({
            "page_id": "253-p901", "case_id": "0597-0001-000253",
            "page_number": 901, "image_path": str(td / "gone.jpg"),
            "image_sha256": "cd" * 32, "status": "ok",
            "completed_at": "2026-11-22T10:00:00+0700",
        }))
        (gone / "transcriptions" / "253-p901.json").write_text(json.dumps({
            "lines": [{"line_number": 1, "text": "строка",
                       "confidence": 0.6, "bbox": [1, 2, 30, 20]}],
        }))

        manifest = td / "index_manifest.json"
        manifest.write_text(json.dumps({
            "253-p900": [str(img), sha, 2488, 3513],
            "253-p901": [str(td / "gone.jpg"), "cd" * 32, None, None],
        }))

        cfg_path = td / "cfg.json"
        cfg_path.write_text(json.dumps({
            "repo_root": str(td),
            "runtime_root": str(td / "rt"),
            "legacy_ocr_root": str(td / "ocr"),
            "legacy_manifest": str(manifest),
            "db_path": str(td / "t.db"),
            "sidecar_mode": "mirror",
            "sources": [],
        }))
        cfg = htr_config.load(cfg_path)

        # --- direct builder --------------------------------------------
        dims = {"253-p900": (2488, 3513), "253-p901": (None, None)}
        sc = page_dir_to_sidecar(page, cfg, dims, origin="imported")
        check(validate_sidecar(sc) == [],
              f"imported sidecar valid: {validate_sidecar(sc)}")
        check(sc["recognition"]["origin"] == "imported", "origin=imported")
        check(sc["recognition"]["import_validation"] == "verified-sha256",
              f"sha re-verified: {sc['recognition']['import_validation']}")
        check(sc["source"]["sha256"] == sha, "recorded sha matches original")
        check(sc["source"]["fond"] == "597" and sc["source"]["delo"] == "253",
              "archival refs derived from page id")
        check(sc["image"] == {"width": 2488, "height": 3513,
                              "orientation": "normal",
                              "coordinate_system":
                                  "original-image-pixels,origin-top-left",
                              "transformations": []},
              f"dims from manifest: {sc['image']}")
        check(sc["lines"][0]["bbox"] == [10, 20, 110, 50],
              f"legacy xywh converted: {sc['lines'][0]['bbox']}")
        check(sc["lines"][0]["text_raw"] == "деревни Волченокъ",
              "raw text preserved (pre-reform)")
        check(sc["lines"][0]["text_norm"] == "деревни волченок",
              f"normalized copy: {sc['lines'][0]['text_norm']!r}")
        check(sc["lines"][0]["verification_status"] == "unverified",
              "imported machine line unverified")
        srcs = [ln["source"] for ln in sc["lines"]]
        check(srcs == ["blla-ocr", "blla-ocr", "stage-b-gap"],
              f"stage-b lines kept with own source tag: {srcs}")
        check(sc["lines"][0]["line_number"] == 1 and
              sc["lines"][2]["line_number"] == 1,
              "original line numbers preserved (review key)")
        check(sc["recognition"]["duration_s"] == 22.5,
              "recognition duration carried over")

        sc_gone = page_dir_to_sidecar(gone, cfg, dims, origin="imported")
        check(sc_gone["recognition"]["import_validation"] ==
              "missing-original-requires-verification",
              f"missing original honestly flagged: "
              f"{sc_gone['recognition']['import_validation']}")
        check(sc_gone["source"]["sha256"] == "cd" * 32,
              "recorded sha retained for missing original")

        # --- batch import through the CLI path --------------------------
        stats = legacy_to_sidecars(cfg, quiet=True)
        check(stats["imported"] == 2 and stats["invalid"] == [],
              f"batch import: {stats}")
        target = Path(cfg["sidecar_root"]) / "cgamos-597" / "000900.jpg.htr.json"
        # logical path falls back to legacy/<pid>/<name> when the image is not
        # under a configured source root
        alt = Path(cfg["sidecar_root"]) / "legacy" / "253-p900" / "000900.jpg.htr.json"
        check(target.exists() or alt.exists(),
              f"sidecar written to mirror: {target if target.exists() else alt}")
        stats2 = legacy_to_sidecars(cfg, quiet=True)
        check(stats2["imported"] == 0 and stats2["skipped_existing"] == 2,
              f"import idempotent: {stats2}")

        # re-import with --force rewrites (revision preserved by writer)
        written = target if target.exists() else alt
        before = read_sidecar(written)
        legacy_to_sidecars(cfg, force=True, quiet=True)
        after = read_sidecar(written)
        check(after["recognition"]["revision"] >= 1 and
              before["schema_version"] == after["schema_version"],
              "forced re-import keeps schema, bumps revision")

    if FAIL:
        print(f"FAIL ({len(FAIL)}):")
        for f in FAIL:
            print("  -", f)
        return 1
    print("test_legacy_import: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
