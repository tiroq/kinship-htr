"""Shared synthetic fixtures for indexer tests (no archival data).

Builds a small sidecar tree covering ok / failed / duplicate pages across
two collections, in the exact schema-1.0.0 shape the worker produces.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from htr_sidecar import atomic_write_json, build_sidecar  # noqa: E402


def _source(page_id: str, collection: str, archive: str, fond: str,
            opis: str, delo: str, case_short: str, page_number: int) -> dict:
    return {
        "source_ref": f"{collection}:{case_short}",
        "page_id": page_id,
        "case_ref": f"{collection}:{case_short}",
        "case_short": case_short,
        "page_number": page_number,
        "original_filename": f"{page_id}.jpg",
        "logical_path": f"{collection}/{page_id}.jpg",
        "image_path": f"/nonexistent/{collection}/{page_id}.jpg",
        "sha256": f"{abs(hash(page_id)) % 10**64:064x}",
        "size_bytes": 12345,
        "collection": collection,
        "archive": archive,
        "fond": fond, "opis": opis, "delo": delo,
        "scan_number": page_number,
        "source_id": collection,
        "root": "/nonexistent",
    }


_RECOGNITION = {
    "engine": "crnn-ctc (PyLaia-inspired CRNN-CTC)",
    "model": "achimrabus/crnn-ctc-russian 'Russian (6.92% CER)'",
    "model_sha256": "74" * 32,
    "segmentation": "kraken-blla",
    "segmentation_sha256": "77" * 32,
    "segmentation_method": "kraken-blla",
    "model_preset": "Russian (6.92% CER)",
    "patch": "polyscriptor-local.patch",
    "device": "cpu",
    "recognized_at": "2026-11-25T12:00:00+0000",
    "duration_s": 24.0,
    "status": "ok",
    "origin": "new",
    "revision": 1,
}

# line_number, text, xywh bbox, confidence
GAVO_LINES = [
    (1, "Той же вотчины деревни Волченокъ крестьянин", [10, 20, 300, 40], 0.91),
    (2, "жена его дѣти ихъ", [10, 70, 300, 35], 0.82),
    (3, "какой-то размытый набор", [10, 120, 300, 35], 0.55),
]
CGAMOS_LINES = [
    (1, "село Протасьево приход", [15, 25, 280, 38], 0.88),
    (2, "Никита Демидовъ жена его Устинья", [15, 75, 280, 38], 0.93),
]


def make_sidecars(sidecar_root: Path) -> dict[str, dict]:
    """Write the fixture sidecar tree; returns page_id -> sidecar dict."""
    out: dict[str, dict] = {}

    def emit(sidecar: dict) -> None:
        pid = sidecar["source"]["page_id"]
        out[pid] = sidecar
        atomic_write_json(Path(sidecar_root) / f"{pid}.htr.json", sidecar)

    gavo = build_sidecar(
        source=_source("g999-p000", "gavo",
                       "ГАВО (Государственный архив Воронежской области)",
                       "И331", "3", "999", "g999", 0),
        image={"width": 1000, "height": 2000},
        recognition=dict(_RECOGNITION, duration_s=25.5),
        lines=[{"line_number": n, "reading_order": n, "text": t,
                "bbox": xywh, "bbox_convention": "xywh", "confidence": c,
                "source": "blla-ocr", "verification_status": "unverified"}
               for n, t, xywh, c in GAVO_LINES])
    emit(gavo)

    cgamos = build_sidecar(
        source=_source("253-p001", "cgamos-597", "ЦГА Москвы",
                       "597", "1", "253", "253", 1),
        image={"width": 2850, "height": 2354},
        recognition=dict(_RECOGNITION),
        lines=[{"line_number": n, "reading_order": n, "text": t,
                "bbox": xywh, "bbox_convention": "xywh", "confidence": c,
                "source": "blla-ocr", "verification_status": "unverified"}
               for n, t, xywh, c in CGAMOS_LINES])
    emit(cgamos)

    failed = build_sidecar(
        source=_source("g998-p000", "gavo",
                       "ГАВО (Государственный архив Воронежской области)",
                       "И331", "3", "998", "g998", 0),
        image={"width": None, "height": None},
        recognition=dict(_RECOGNITION, status="failed", origin="new",
                         error="RuntimeError: segmentation produced no lines"),
        lines=[])
    emit(failed)

    dup = build_sidecar(
        source=_source("g997-p000", "gavo",
                       "ГАВО (Государственный архив Воронежской области)",
                       "И331", "3", "997", "g997", 0),
        image={"width": 1000, "height": 2000},
        recognition=dict(_RECOGNITION, status="duplicate",
                         origin="deduplicated",
                         error="content identical to g999-p000; inference skipped"),
        lines=[],
        content={"sha256": gavo["source"]["sha256"],
                 "canonical_page_id": "g999-p000",
                 "duplicate_of": "g999-p000"})
    emit(dup)
    return out


def build_test_db(db_path: Path, sidecar_root: Path):
    """Create the fixture DB via the real rebuild path; returns db module."""
    from htr_search import db as dbmod
    saved_selection = dbmod.SELECTION
    dbmod.SELECTION = Path("/nonexistent-selection.json")
    try:
        stats = dbmod.build(db_path, sidecar_root=sidecar_root,
                            import_legacy=False)
    finally:
        dbmod.SELECTION = saved_selection
    return stats
