"""Import pre-existing recognition artifacts as portable sidecars.

Covers every completed page under the legacy OCR root (work/index/ocr/<pid>/:
CGAMOS 597-1-253/255/256/258, GAVO д.611, benchmark-reused pages), converting
meta.json + transcriptions/*.json + stage_b.json into schema-1.0.0 sidecars
without rerunning HTR.

Provenance validation for every import (§ reuse contract):
  * the original image is re-hashed when present and compared with the
    recorded image_sha256 -> "verified-sha256";
  * hash mismatch -> "mismatch-requires-verification" (artifacts preserved,
    flagged, never silently attributed as verified);
  * original missing -> "missing-original-requires-verification".
Imported human-reviewed data is NOT touched here: reviews live in the DB,
keyed by stable (page_id, line_number), and survive rebuilds.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from htr_config import ensure_runtime_dirs, load as load_config
from htr_sidecar import (build_sidecar, sidecar_path_for, validate_sidecar,
                         write_sidecar)
from htr_search.normalize import normalize

# page_id patterns -> archival identity (historical conventions)
def _identity_for(page_id: str, image_path: str, cfg: dict) -> dict:
    short = page_id.split("-")[0]
    if short.isdigit():
        collection, archive = "cgamos-597", "ЦГА Москвы"
        fond, opis, delo = "597", "1", short
        case_ref = f"cgamos:{short}"
    elif short.startswith("g") and short[1:].isdigit():
        collection = "gavo"
        archive = "ГАВО (Государственный архив Воронежской области)"
        fond, opis, delo = "И331", "3", short[1:]
        case_ref = f"gavo-I331-3-{delo}"
    else:
        collection, archive = "unknown", None
        fond = opis = delo = None
        case_ref = f"legacy:{page_id}"
    try:
        page_number = int(page_id.split("p")[-1])
    except ValueError:
        page_number = 0

    # place the image under its configured source root when possible
    source_id, logical = None, None
    img = Path(image_path)
    best = ""
    for src in cfg["sources"]:
        root = str(Path(src["root"]))
        if image_path.startswith(root + "/") and len(root) > len(best):
            best, source_id = root, src["id"]
            logical = f"{src['id']}/{image_path[len(root) + 1:]}"
    if logical is None:
        logical = f"legacy/{page_id}/{img.name}"

    return {
        "source_id": source_id or "legacy",
        "root": best or str(img.parent),
        "collection": collection, "archive": archive,
        "fond": fond, "opis": opis, "delo": delo,
        "case_ref": case_ref, "case_short": short,
        "page_number": page_number,
        "original_filename": img.name,
        "logical_path": logical,
        "image_path": image_path,
    }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def page_dir_to_sidecar(page_dir: Path, cfg: dict, dims: dict,
                        origin: str = "imported",
                        inv_rec: dict | None = None) -> dict | None:
    """Build (but do not write) the sidecar for one recognition page dir.

    origin: "imported" (pre-existing artifacts) or "new" (fresh HTR).
    inv_rec: inventory state record for the original image — when present its
    trusted identity (sha/size/dims/archival refs) wins over page-id patterns.
    """
    meta_p = page_dir / "meta.json"
    if not meta_p.exists():
        return None
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    pid = meta.get("page_id") or page_dir.name
    status = meta.get("status", "failed")
    image_path = meta.get("image_path", "")
    src = _identity_for(pid, image_path, cfg)
    if inv_rec:
        src.update({
            "fond": inv_rec.get("fond"), "opis": inv_rec.get("opis"),
            "delo": inv_rec.get("delo"),
            "collection": inv_rec.get("collection") or src["collection"],
            "archive": inv_rec.get("archive") or src["archive"],
            "case_ref": inv_rec.get("case_ref") or src["case_ref"],
            "case_short": inv_rec.get("case_short") or src["case_short"],
            "page_number": inv_rec.get("page_number", src["page_number"])
            if inv_rec.get("page_number") is not None else src["page_number"],
            "logical_path": inv_rec.get("logical_path") or src["logical_path"],
            "source_id": inv_rec.get("source_id") or src["source_id"],
            "root": inv_rec.get("root") or src["root"],
        })
        if inv_rec.get("scan_number") is not None:
            src["scan_number"] = inv_rec["scan_number"]
    w, h = dims.get(pid, (None, None))
    if inv_rec:
        w = inv_rec.get("width") or w
        h = inv_rec.get("height") or h

    # provenance validation (import only; new recognition is self-verifying)
    recorded_sha = meta.get("image_sha256")
    size_bytes = None
    validation = None
    if origin == "imported":
        inv_sha = (inv_rec or {}).get("sha256")
        if image_path and Path(image_path).exists():
            p = Path(image_path)
            size_bytes = p.stat().st_size
            actual = _sha256_file(p)
            if recorded_sha and actual == recorded_sha:
                validation = "verified-sha256"
            elif recorded_sha is None and inv_sha and actual == inv_sha:
                validation = "verified-sha256"
            elif recorded_sha is None:
                validation = "unverifiable-requires-verification"
            else:
                validation = "mismatch-requires-verification"
            sha = recorded_sha or inv_sha or actual
        else:
            validation = "missing-original-requires-verification"
            sha = inv_sha or recorded_sha
        if inv_rec and inv_rec.get("size_bytes"):
            size_bytes = inv_rec["size_bytes"]
    else:
        sha = (inv_rec or {}).get("sha256") or recorded_sha
        size_bytes = (inv_rec or {}).get("size_bytes")
        if image_path and Path(image_path).exists() and size_bytes is None:
            size_bytes = Path(image_path).stat().st_size

    lines = []
    tjson = page_dir / "transcriptions" / f"{pid}.json"
    if status == "ok" and tjson.exists():
        tj = json.loads(tjson.read_text(encoding="utf-8"))
        for ln in tj.get("lines", []):
            lines.append({
                "line_number": ln.get("line_number"),
                "reading_order": ln.get("line_number"),
                "text": ln.get("text", ""),
                "text_norm": normalize(ln.get("text", "")),
                "bbox": ln["bbox"], "bbox_convention": "xywh",
                "confidence": ln.get("confidence"),
                "source": "blla-ocr",
                "verification_status": "unverified",
            })
    sb = page_dir / "stage_b.json"
    if status == "ok" and sb.exists():
        for ln in json.loads(sb.read_text(encoding="utf-8")).get("lines", []):
            lines.append({
                "line_number": ln.get("line_number"),
                "reading_order": ln.get("line_number"),
                "text": ln.get("text", ""),
                "text_norm": normalize(ln.get("text", "")),
                "bbox": ln["bbox"], "bbox_convention": "xywh",
                "confidence": ln.get("confidence"),
                "source": "stage-b-gap",
                "verification_status": "unverified",
            })
    for i, ln in enumerate(lines, start=1):
        if ln.get("line_number") is None:
            ln["line_number"] = i
            ln["reading_order"] = i

    recognition = {
        "engine": meta.get("engine", "crnn-ctc (PyLaia-inspired CRNN-CTC)"),
        "engine_version": None,
        "model": meta.get("model"),
        "model_sha256": meta.get("model_sha256"),
        "segmentation": meta.get("segmentation"),
        "segmentation_sha256": meta.get("blla_sha256"),
        "segmentation_method": "kraken-blla",
        "model_preset": "Russian (6.92% CER)",
        "patch": meta.get("patch"),
        "device": meta.get("device", "cpu"),
        "recognized_at": meta.get("completed_at"),
        "duration_s": meta.get("elapsed_s"),
        "status": status,
        "origin": origin,
        "reused_from_benchmark": meta.get("reused_from_benchmark", False),
        "error": meta.get("error"),
        "revision": 1,
        "env": meta.get("env"),
    }
    if validation is not None:
        recognition["import_validation"] = validation
    source = {
        "source_ref": src["case_ref"],
        "page_id": pid,
        "case_ref": src["case_ref"],
        "case_short": src["case_short"],
        "page_number": src["page_number"],
        "original_filename": src["original_filename"],
        "logical_path": src["logical_path"],
        "image_path": src["image_path"],
        "sha256": sha,
        "size_bytes": size_bytes,
        "collection": src["collection"],
        "archive": src["archive"],
        "fond": src["fond"], "opis": src["opis"], "delo": src["delo"],
        "scan_number": src.get("scan_number", src["page_number"]),
        "source_id": src["source_id"],
        "root": src["root"],
    }
    return build_sidecar(
        source=source,
        image={"width": w, "height": h},
        recognition=recognition,
        lines=lines,
        content={"sha256": sha, "canonical_page_id": pid,
                 "duplicate_of": None},
    )


def legacy_to_sidecars(cfg: dict | None = None, *, force: bool = False,
                       quiet: bool = False) -> dict:
    """Convert all legacy page dirs to sidecars (idempotent)."""
    cfg = cfg or load_config()
    ensure_runtime_dirs(cfg)
    dims = {}
    try:
        for pid, (_p, _sha, w, h) in json.loads(
                Path(cfg["legacy_manifest"]).read_text(encoding="utf-8")).items():
            dims[pid] = (w, h)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    state_files: dict = {}
    try:
        from htr_inventory import load_state
        state_files = load_state(cfg).get("files", {})
    except ImportError:
        pass

    stats = {"imported": 0, "skipped_existing": 0, "failed_meta": 0,
             "invalid": []}
    ocr_root = Path(cfg["legacy_ocr_root"])
    if not ocr_root.exists():
        return stats
    for page_dir in sorted(ocr_root.glob("*/")):
        if page_dir.name.startswith("_tmp"):
            continue
        if not (page_dir / "meta.json").exists():
            continue
        try:
            meta = json.loads((page_dir / "meta.json").read_text(
                encoding="utf-8"))
            inv_rec = state_files.get(meta.get("image_path", ""))
            sidecar = page_dir_to_sidecar(page_dir, cfg, dims,
                                          origin="imported",
                                          inv_rec=inv_rec)
        except (json.JSONDecodeError, KeyError, OSError) as e:
            stats["failed_meta"] += 1
            stats["invalid"].append(f"{page_dir.name}: {e!r}")
            continue
        if sidecar is None:
            continue
        target = sidecar_path_for(Path(sidecar["source"]["image_path"]),
                                  cfg, sidecar["source"])
        if target.exists() and not force:
            stats["skipped_existing"] += 1
            continue
        problems = validate_sidecar(sidecar)
        if problems:
            stats["invalid"].append(f"{page_dir.name}: {problems[:3]}")
            continue
        write_sidecar(target, sidecar)
        stats["imported"] += 1
    if not quiet:
        print(json.dumps(stats, ensure_ascii=False, indent=1))
    return stats


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None)
    ap.add_argument("--force", action="store_true",
                    help="rewrite sidecars even if present")
    ns = ap.parse_args()
    cfg = load_config(ns.config)
    legacy_to_sidecars(cfg, force=ns.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
