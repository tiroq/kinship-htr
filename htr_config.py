"""Single-file configuration for the incremental HTR indexer.

Reads one JSON file (standard library only, no config framework):

  1. $HTR_INDEX_CONFIG if set, else
  2. <repo>/htr-index.json if present, else
  3. built-in defaults that reproduce the historical hard-coded paths.

All paths are resolved relative to the repository root unless absolute.
`htr-index.example.json` is committed; the real `htr-index.json` is
gitignored (local paths stay out of Git).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

DEFAULTS: dict = {
    # --- locations -------------------------------------------------------
    "repo_root": str(REPO_ROOT),
    "runtime_root": "work/indexer",          # state, logs, sidecars, new OCR
    "legacy_ocr_root": "work/index/ocr",     # pre-indexer recognition output
    "legacy_manifest": "work/index_manifest.json",
    "db_path": "work/index/htr.db",          # unified searchable index
    "polyscriptor_dir": "polyscriptor",
    "python_env": "/Users/mysterx/miniconda3/envs/htrbench/bin/python",
    # --- recognition -----------------------------------------------------
    "engine": "crnn-ctc",
    "model_preset": "Russian (6.92% CER)",   # polyscriptor preset name
    "segmentation_method": "kraken-blla",
    "device": "cpu",
    "do_stage_b": True,
    # --- resources -------------------------------------------------------
    "threads": 1,
    "workers": 1,                            # recognition processes (keep 1)
    "nice": 10,
    "page_timeout_s": 900,
    "poll_interval_s": 60,
    "round_pages": 20,                       # pages per spawned slice
    "min_free_gb": 5.0,
    "max_attempts": 3,                       # attempts before a page is parked
    # --- sidecars --------------------------------------------------------
    "sidecar_mode": "mirror",                # "mirror" | "inline"
    # --- sources ---------------------------------------------------------
    # priority: lower = processed earlier; discovery is round-robin across
    # sources so a large backlog cannot starve a small/new one.
    "sources": [
        {
            "id": "gavo",
            "root": "/Users/mysterx/Downloads/kinship-gavo",
            "collection": "gavo",
            "archive": "ГАВО (Государственный архив Воронежской области)",
            "include": ["I-331-op3-d*/originals/*.jpg"],
            "manifest": "gavo",              # per-case manifest.csv + metadata.json
            "priority": 2,
        },
        {
            "id": "torrents",
            "root": "/Volumes/T7 Shield/Kinship/torrents",
            "collection": "torrent",
            "archive": "ГАВО (Государственный архив Воронежской области)",
            "include": ["**/*.jpg", "**/*.jpeg"],
            "manifest": "filename",          # Ф.И18.Оп.1.Д.0131.00001.jpg
            "priority": 3,
        },
        {
            "id": "cgamos-597",
            "root": "/Users/mysterx/dev/kinship-case/sources/cgamos-online",
            "collection": "cgamos-597",
            "archive": "ЦГА Москвы",
            "include": ["597-1-*/originals/*.jpg"],
            "manifest": "legacy",            # work/index_manifest.json shas
            "priority": 4,
        },
        {
            "id": "cgamos-misc",
            "root": "/Users/mysterx/dev/kinship-case/sources/cgamos-online",
            "collection": "cgamos-misc",
            "archive": "ЦГА Москвы",
            "include": ["*.png", "*.jpg",
                        "opis-t*-pages/*.png", "gk-pages/*.png"],
            "manifest": "none",
            "priority": 9,
        },
    ],
    # never scanned as archival originals
    "exclude_dir_names": ["crops", "crops1998", "crops2000", "crops2001",
                          "crops2011", "crops2014", "crops2019", "crops2024",
                          "crops346", "crops350", "crops358", "crops3590",
                          "crops360", "crops361", "crops404", "crops412",
                          "crops4635", "crops-mil01", "thumbs", "review",
                          "sheets", "draftsheets", "ocr", "recovery",
                          "work", "__pycache__"],
    "image_extensions": [".jpg", ".jpeg", ".png", ".tif", ".tiff"],
}


def _resolve(value: str, root: Path) -> str:
    p = Path(os.path.expanduser(value))
    return str(p if p.is_absolute() else (root / p))


def load(path: str | Path | None = None) -> dict:
    """Load config merged over DEFAULTS; returns a dict with absolute paths."""
    cfg = dict(DEFAULTS)
    if path is None:
        path = os.environ.get("HTR_INDEX_CONFIG")
    if path is None:
        candidate = REPO_ROOT / "htr-index.json"
        path = str(candidate) if candidate.exists() else None
    if path is not None:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        cfg.update(raw)
    root = Path(cfg["repo_root"])
    for key in ("runtime_root", "legacy_ocr_root", "legacy_manifest",
                "db_path", "polyscriptor_dir"):
        cfg[key] = _resolve(cfg[key], root)
    cfg["config_path"] = str(path) if path else "<defaults>"
    cfg["sidecar_root"] = str(Path(cfg["runtime_root"]) / "sidecars")
    cfg["ocr_root"] = str(Path(cfg["runtime_root"]) / "ocr")
    cfg["state_path"] = str(Path(cfg["runtime_root"]) / "state.json")
    cfg["logs_dir"] = str(Path(cfg["runtime_root"]) / "logs")
    cfg["heartbeat_dir"] = str(Path(cfg["runtime_root"]) / "heartbeat")
    cfg["pid_path"] = str(Path(cfg["runtime_root"]) / "worker.pid")
    cfg["lock_path"] = str(Path(cfg["runtime_root"]) / "worker.lock")
    cfg["progress_log"] = str(Path(cfg["logs_dir"]) / "progress.jsonl")
    # durable human-review store (source of truth; DB reviews are a view)
    cfg["reviews_path"] = str(Path(cfg["runtime_root"]) / "reviews" /
                              "reviews.jsonl")
    return cfg


def ensure_runtime_dirs(cfg: dict) -> None:
    for key in ("runtime_root", "sidecar_root", "ocr_root", "logs_dir",
                "heartbeat_dir"):
        Path(cfg[key]).mkdir(parents=True, exist_ok=True)
