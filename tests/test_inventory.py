#!/usr/bin/env python3
"""Self-contained tests for source discovery/inventory (htr_inventory).

Builds a synthetic source tree (GAVO-style case with trusted completion
manifest, generic files, torrent-style names, AppleDouble/.part/crops
decoys) in a temp dir and verifies: discovery filters, stability across
two polls, trusted-manifest confirmation, SHA/dimension capture, stable
page-id allocation, and identity derivation.  No archival datasets needed.

Run:  htrbench-python tests/test_inventory.py   (pytest-compatible)
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import htr_config
import htr_inventory as inv

FAIL: list[str] = []


def check(cond, msg):
    if cond:
        print(f"PASS {msg}")
    else:
        FAIL.append(msg)
        print(f"FAIL {msg}")


def _jpeg(path: Path, color=(200, 100, 50)) -> None:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color).save(path, "JPEG")


def _write_cfg(td: Path) -> dict:
    cfg_path = td / "cfg.json"
    cfg_path.write_text(json.dumps({
        "repo_root": str(td),
        "runtime_root": str(td / "rt"),
        "legacy_ocr_root": str(td / "no-ocr"),
        "legacy_manifest": str(td / "no-manifest.json"),
        "db_path": str(td / "t.db"),
        "sidecar_mode": "mirror",
        "sources": [
            {"id": "gavo", "root": str(td / "src-gavo"),
             "collection": "gavo", "archive": "ГАВО",
             "include": ["I-331-op3-d*/originals/*.jpg"],
             "manifest": "gavo", "priority": 2},
            {"id": "torrents", "root": str(td / "src-tor"),
             "collection": "torrent", "archive": "ГАВО",
             "include": ["**/*.jpg"], "manifest": "filename",
             "priority": 3},
            {"id": "gen", "root": str(td / "src-gen"),
             "collection": "gen", "archive": None,
             "include": ["**/*.jpg"], "manifest": "none",
             "priority": 9},
        ],
    }, ensure_ascii=False))
    return htr_config.load(cfg_path)


def main() -> int:
    with tempfile.TemporaryDirectory() as tds:
        td = Path(tds)

        # --- synthetic sources -----------------------------------------
        gavo_orig = td / "src-gavo" / "I-331-op3-d999" / "originals"
        _jpeg(gavo_orig / "И331Оп3Д999_0000.jpg")
        _jpeg(gavo_orig / "И331Оп3Д999_0001.jpg")
        (gavo_orig / "._И331Оп3Д999_0000.jpg").write_bytes(b"appledouble")
        (gavo_orig / "И331Оп3Д999_0002.jpg.part").write_bytes(b"partial")
        sha0 = "11" * 32
        with (td / "src-gavo" / "I-331-op3-d999" / "manifest.csv").open(
                "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["seq", "file", "size_bytes", "width", "height",
                        "sha256", "jpeg_verify"])
            w.writerow([0, "И331Оп3Д999_0000.jpg",
                        (gavo_orig / "И331Оп3Д999_0000.jpg").stat().st_size,
                        8, 8, sha0, "JPEG ok"])
            w.writerow([1, "И331Оп3Д999_0001.jpg",
                        (gavo_orig / "И331Оп3Д999_0001.jpg").stat().st_size,
                        8, 8, "22" * 32, "JPEG ok"])
        (td / "src-gavo" / "I-331-op3-d999" / "metadata.json").write_text(
            json.dumps({"expected": 2, "downloaded": 2, "jpegVerified": 2,
                        "bad": [], "sha_mismatches": [],
                        "missingIndices": []}))

        tor = td / "src-tor" / "ГАВО - ф. И18 оп. 1 д. 131"
        _jpeg(tor / "Ф.И18.Оп.1.Д.0131.00001.jpg")
        (tor / "INDEX.txt").write_text("index")

        gen = td / "src-gen"
        _jpeg(gen / "a.jpg")
        _jpeg(gen / "crops" / "crop.jpg")       # excluded dir name
        _jpeg(gen / "b.jpg")

        cfg = _write_cfg(td)

        # --- poll 1: discovery -----------------------------------------
        s1 = inv.poll(cfg)
        check(s1["discovered"] == 5,
              f"discovered exactly the 5 real images (got {s1['discovered']})")
        state = inv.load_state(cfg)
        files = state["files"]
        check(not any(Path(k).name.startswith("._") for k in files),
              "AppleDouble files never inventoried")
        check(not any(k.endswith(".part") for k in files),
              ".part downloads never inventoried")
        check(not any("/crops/" in k for k in files),
              "excluded directories never inventoried")
        check(s1["errors"] == [], f"no source errors: {s1['errors']}")

        gavo_recs = [r for r in files.values() if r["source_id"] == "gavo"]
        # trusted completion manifest: confirmed on FIRST poll
        check(all(r["confirmed_at"] for r in gavo_recs) and len(gavo_recs) == 2,
              "GAVO complete-case files confirmed on first poll")
        r0 = next(r for r in gavo_recs if r["scan_number"] == 0)
        check(r0["sha256"] == sha0, "trusted manifest sha adopted")
        check(r0["width"] == 8 and r0["height"] == 8, "manifest dims adopted")
        check(r0["page_id"] == "g999-p000", f"page id: {r0['page_id']}")
        check(r0["fond"] == "И331" and r0["opis"] == "3" and r0["delo"] == "999",
              f"archival refs: {r0['fond']}/{r0['opis']}/{r0['delo']}")
        check(r0["case_ref"] == "gavo-I331-3-999", f"case_ref {r0['case_ref']}")

        tor_recs = [r for r in files.values() if r["source_id"] == "torrents"]
        check(len(tor_recs) == 1 and tor_recs[0]["page_id"] == "i18-1-131-p00001",
              f"torrent page id: {tor_recs and tor_recs[0]['page_id']}")
        check(tor_recs[0]["fond"] == "И18" and tor_recs[0]["delo"] == "131",
              "torrent archival refs")

        gen_recs = sorted((r for r in files.values()
                           if r["source_id"] == "gen"),
                          key=lambda r: r["original_filename"])
        check(len(gen_recs) == 2, f"generic files found: {len(gen_recs)}")
        check(all(not r["confirmed_at"] for r in gen_recs),
              "generic files NOT confirmed after one observation")
        pns = sorted(r["page_number"] for r in gen_recs)
        check(pns == [1, 2], f"page numbers allocated uniquely: {pns}")
        ids1 = {k: r["page_id"] for k, r in files.items()
                if r["source_id"] == "gen"}

        # --- poll 2: stability confirmation -----------------------------
        s2 = inv.poll(cfg)
        state = inv.load_state(cfg)
        files = state["files"]
        gen2 = [dict(r, path=k) for k, r in files.items()
                if r["source_id"] == "gen"]
        check(all(r["confirmed_at"] for r in gen2),
              "generic files confirmed on second unchanged observation")
        check(all(r["sha256"] and len(r["sha256"]) == 64 for r in gen2),
              "sha256 computed at confirmation")
        check(all(r["decode_ok"] for r in gen2), "decode check recorded")
        check(all(r["width"] == 8 for r in gen2), "dimensions captured")
        ids2 = {r["path"]: r["page_id"] for r in gen2}
        check(ids1 == ids2, "page ids stable across polls")
        check(state["alloc"] == {"gen-root": 2},
              f"allocation counter persisted once: {state['alloc']}")

        # --- stability reset: an in-progress file must re-confirm -------
        target = next(r["path"] for r in gen2)
        from PIL import Image
        Image.new("RGB", (16, 16), (10, 20, 30)).save(target, "JPEG")
        inv.poll(cfg)
        state = inv.load_state(cfg)
        rec = state["files"][target]
        check(rec["confirmed_at"] is None and rec["obs"] == 1,
              "size change resets confirmation (in-progress guard)")
        inv.poll(cfg)
        state = inv.load_state(cfg)
        rec = state["files"][target]
        check(rec["confirmed_at"] is not None,
              "re-confirmed after change settles")
        check(state["files"][target]["page_id"] == ids2[target],
              "page id survives content growth")

        # --- stable_files ordering --------------------------------------
        stable = inv.stable_files(cfg)
        check(len(stable) == 5, f"5 stable files total: {len(stable)}")
        check(all("path" in r for r in stable), "stable records carry path")

    if FAIL:
        print(f"FAIL ({len(FAIL)}):")
        for f in FAIL:
            print("  -", f)
        return 1
    print("test_inventory: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
