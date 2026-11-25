"""Portable per-image HTR sidecars (schema kinship-htr-sidecar 1.0.0).

Contract (full text: docs/htr-sidecar-schema.md):

* One JSON file per original image: `<original-filename>.htr.json`.
  Written either beside the source image (mode "inline") or into a mirrored
  tree that preserves the logical source path (mode "mirror", default).
* Bounding boxes are ALWAYS [x_min, y_min, x_max, y_max] in original-image
  pixel coordinates (top-left origin).  Polyscriptor/transcription outputs
  use [x, y, width, height] — converted here via xywh_to_xyxy/xyxy_to_xywh.
* Raw diplomatic text (pre-reform orthography preserved) is separate from
  normalized retrieval text; normalization is retrieval-only.
* polygon/baseline stay null unless genuinely available — never invented.
* Writes are atomic (temp file + os.replace); a valid sidecar is never
  partially written.  Reprocessing preserves the previous revision under
  `revisions/`.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

SCHEMA_NAME = "kinship-htr-sidecar"
SCHEMA_VERSION = "1.0.0"

BBOX_CONVENTION = "xyxy-original-image-pixels"


# ---------------------------------------------------------------- conversions

def xywh_to_xyxy(bbox: list) -> list:
    """[x, y, w, h] -> [x_min, y_min, x_max, y_max]."""
    x, y, w, h = bbox
    return [x, y, x + w, y + h]


def xyxy_to_xywh(bbox: list) -> list:
    """[x_min, y_min, x_max, y_max] -> [x, y, w, h]."""
    x0, y0, x1, y1 = bbox
    return [x0, y0, x1 - x0, y1 - y0]


# Canonical token for the one patch this pipeline uses.  Historical provenance
# strings label it either "bench-patch.diff (...)" or "polyscriptor-local.patch
# (...)" while describing the IDENTICAL change; normalizing keeps recognition
# configuration fingerprints comparable across legacy and new artifacts.
PATCH_CANONICAL = "polyscriptor-local.patch@52524ed(min_height40->15,min_width16)"

# Behaviour-determining configuration keys only.  Keys must be present in both
# sidecar recognition blocks and DB pages.provenance JSON so a fingerprint can
# be recomputed from either side (dedup compares them).
FP_KEYS = ("engine", "model", "model_sha256", "segmentation",
           "segmentation_sha256", "device", "patch")


def normalize_patch(patch) -> str | None:
    if patch is None:
        return None
    s = str(patch)
    if any(t in s for t in ("min_height", "min line", "40->15",
                            "bench-patch", "polyscriptor-local")):
        return PATCH_CANONICAL
    return s


def config_fingerprint(recognition: dict) -> str:
    """Stable sha256 over the recognition configuration (dedup key part).

    Only behaviour-determining keys; `patch` is normalized so legacy
    provenance ("bench-patch.diff …") and new provenance
    ("polyscriptor-local.patch …") fingerprint identically.
    """
    payload = json.dumps(
        {k: (normalize_patch(recognition.get(k)) if k == "patch"
             else recognition.get(k)) for k in sorted(FP_KEYS)},
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fingerprint_from_provenance(provenance: dict) -> str | None:
    """Recompute the config fingerprint from a DB pages.provenance dict.

    Returns None when required keys are absent (caller must then treat the
    configuration as unknown — never as a dedup match).
    """
    if not isinstance(provenance, dict):
        return None
    missing = [k for k in FP_KEYS if k not in provenance]
    if missing:
        return None
    return config_fingerprint(provenance)


# ---------------------------------------------------------------- sidecar build

def build_sidecar(*, source: dict, image: dict, recognition: dict,
                  lines: list, content: dict | None = None,
                  previous_revision: int | None = None) -> dict:
    """Assemble a sidecar dict.

    source:   identity of the original file (see docs/htr-sidecar-schema.md)
    image:    {"width", "height", ...}
    recognition: provenance + status + timing
    lines:    recognition lines; each may carry bbox in EITHER convention
              via `bbox_convention` (default xywh, converted to xyxy here)
    content:  {"sha256", "canonical_page_id", "duplicate_of"}
    """
    out_lines = []
    for i, ln in enumerate(lines, start=1):
        conv = ln.get("bbox_convention", "xywh")
        bbox = ln["bbox"]
        bbox_xyxy = xywh_to_xyxy(bbox) if conv == "xywh" else list(bbox)
        out_lines.append({
            "line_id": f"{source['page_id']}-r{recognition.get('revision', 1)}"
                       f"-{i:04d}",
            "line_number": ln.get("line_number", i),
            "region_id": ln.get("region_id"),
            "reading_order": ln.get("reading_order", i),
            "text_raw": ln.get("text", ln.get("text_raw", "")),
            "text_norm": ln.get("text_norm", ""),
            "bbox": bbox_xyxy,
            "bbox_convention": BBOX_CONVENTION,
            "polygon": ln.get("polygon"),
            "baseline": ln.get("baseline"),
            "confidence": ln.get("confidence"),
            "verification_status": ln.get("verification_status", "unverified"),
            "source": ln.get("source", "blla-ocr"),
        })
    rec = dict(recognition)
    rec.setdefault("revision", 1)
    rec["config_fingerprint"] = config_fingerprint(rec)
    sidecar = {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "image": {
            "width": image.get("width"),
            "height": image.get("height"),
            "orientation": image.get("orientation", "normal"),
            "coordinate_system": "original-image-pixels,origin-top-left",
            "transformations": image.get("transformations", []),
        },
        "recognition": rec,
        "lines": out_lines,
        "content_group": content or {
            "sha256": source.get("sha256"),
            "canonical_page_id": source["page_id"],
            "duplicate_of": None,
        },
        "artifact_integrity": {
            "written_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "previous_revision": previous_revision,
        },
    }
    return sidecar


# ---------------------------------------------------------------- paths

def sidecar_rel_path(image_path: Path, source_root: Path) -> Path:
    """Logical relative path of an image under its source root.

    Does NOT resolve symlinks: GAVO's root is a symlink to an external drive
    and the mirror must preserve the logical (configured) path.
    """
    p = Path(os.path.normpath(str(image_path)))
    root = Path(os.path.normpath(str(source_root)))
    try:
        return p.relative_to(root)
    except ValueError:
        return Path(p.name)


def sidecar_path_for(image_path: Path, cfg: dict, source: dict,
                     mode: str | None = None) -> Path:
    """Where the sidecar for `image_path` lives (mirror or inline).

    Mirror layout: <sidecar_root>/<logical_path>.htr.json — the logical path
    already carries the source id prefix (e.g. gavo/I-331-op3-d605/...).
    """
    mode = mode or cfg.get("sidecar_mode", "mirror")
    if mode == "inline":
        return Path(str(image_path) + ".htr.json")
    logical = source.get("logical_path")
    if not logical:
        logical = str(sidecar_rel_path(Path(image_path),
                                       Path(source.get("root", "."))))
        logical = f"{source.get('source_id', 'unknown')}/{logical}"
    return Path(cfg["sidecar_root"]) / (logical + ".htr.json")


# ---------------------------------------------------------------- io

def atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def write_sidecar(path: Path, sidecar: dict) -> None:
    """Atomic write; if a different valid revision exists, preserve it under
    revisions/<name>.rev<N>.json before replacing."""
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = None
        if old is not None and old != sidecar:
            rev = int(old.get("recognition", {}).get("revision", 1))
            archive = path.parent / "revisions"
            archive.mkdir(parents=True, exist_ok=True)
            keep = archive / f"{path.name}.rev{rev}.json"
            if not keep.exists():
                keep.write_text(json.dumps(old, ensure_ascii=False, indent=1),
                                encoding="utf-8")
            new_rev = rev + 1
            sidecar.setdefault("artifact_integrity", {})[
                "previous_revision"] = rev
            sidecar["recognition"]["revision"] = new_rev
            # line ids are stable WITHIN a recognition revision: re-stamp them
            # so ids always carry the revision the sidecar declares
            # (otherwise a bumped revision would keep -r1- ids — inconsistent
            # revision identifiers).
            page_id = sidecar.get("source", {}).get("page_id", "")
            for i, ln in enumerate(sidecar.get("lines", []), start=1):
                ln["line_id"] = f"{page_id}-r{new_rev}-{i:04d}"
    atomic_write_json(path, sidecar)


def read_sidecar(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- validation

REQUIRED_TOP = ("schema", "schema_version", "source", "image",
                "recognition", "lines", "content_group", "artifact_integrity")
REQUIRED_SOURCE = ("source_ref", "page_id", "original_filename",
                   "logical_path", "image_path", "sha256", "size_bytes",
                   "collection", "case_ref", "case_short", "page_number")
REQUIRED_REC = ("engine", "model", "config_fingerprint", "status", "origin")


def validate_sidecar(sidecar: dict) -> list[str]:
    """Return a list of problems; empty list = valid."""
    problems: list[str] = []
    for k in REQUIRED_TOP:
        if k not in sidecar:
            problems.append(f"missing top-level key {k!r}")
    if sidecar.get("schema") != SCHEMA_NAME:
        problems.append(f"schema name {sidecar.get('schema')!r}")
    if sidecar.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version {sidecar.get('schema_version')!r}")
    src = sidecar.get("source", {})
    for k in REQUIRED_SOURCE:
        if k not in src:
            problems.append(f"missing source.{k}")
    rec = sidecar.get("recognition", {})
    for k in REQUIRED_REC:
        if k not in rec:
            problems.append(f"missing recognition.{k}")
    img = sidecar.get("image", {})
    for k in ("width", "height", "coordinate_system"):
        if k not in img:
            problems.append(f"missing image.{k}")
    seen: set[str] = set()
    for i, ln in enumerate(sidecar.get("lines", [])):
        lid = ln.get("line_id")
        if not lid:
            problems.append(f"lines[{i}]: missing line_id")
        elif lid in seen:
            problems.append(f"lines[{i}]: duplicate line_id {lid}")
        else:
            seen.add(lid)
        bbox = ln.get("bbox")
        if not (isinstance(bbox, list) and len(bbox) == 4):
            problems.append(f"lines[{i}]: bbox not [x0,y0,x1,y1]")
        elif ln.get("bbox_convention") != BBOX_CONVENTION:
            problems.append(f"lines[{i}]: bbox_convention "
                            f"{ln.get('bbox_convention')!r}")
        elif not (bbox[0] <= bbox[2] and bbox[1] <= bbox[3]):
            problems.append(f"lines[{i}]: bbox not ordered {bbox}")
        if "text_raw" not in ln or "text_norm" not in ln:
            problems.append(f"lines[{i}]: missing text_raw/text_norm")
        if not isinstance(ln.get("line_number"), int):
            problems.append(f"lines[{i}]: line_number not an integer")
        if "confidence" in ln and ln["confidence"] is not None and not (
                0.0 <= ln["confidence"] <= 1.0):
            problems.append(f"lines[{i}]: confidence out of range")
    if not isinstance(sidecar.get("content_group", {}).get("sha256"), str):
        problems.append("content_group.sha256 not a string")
    return problems
