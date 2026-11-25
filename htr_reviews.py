"""Durable human-review store — independent of the SQLite search index.

The SQLite `reviews` table is a MATERIALIZED VIEW of this store.  The store
is an append-only JSONL file (one record per line, atomic append + fsync);
loading folds records by `record_id` (latest `updated_at` wins), so review
edits keep their history and a complete database loss is recoverable:

    reviews.jsonl  --(load + match)-->  reviews table (active|unresolved)

Binding (what a review is attached to — NEVER auto-transferred):
  * `image_sha256`        — the original image the human looked at;
  * `recognition_revision`— the recognition revision reviewed;
  * `line_number`         — line number within that revision;
  * `bbox`                — exact line geometry (xyxy, original pixels);
  * `verified_text`       — the human reading itself.

Matching rule (`match_review_record`): a record is `active` (bound to a
current line) only when image sha, revision, line_number AND bbox all match
the current recognition result.  Anything else becomes `unresolved` with an
explicit reason — a review is never deleted and never re-attached to a
different spatial region or a different recognition revision.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from htr_sidecar import xywh_to_xyxy

REVIEW_SCHEMA = "kinship-htr-review"
REVIEW_SCHEMA_VERSION = "1.0.0"

MATCH_REASONS = ("page_missing", "image_changed", "revision_changed",
                 "line_missing", "geometry_changed", "superseded",
                 "pending_match", "legacy-unmatched")


# ---------------------------------------------------------------- store I/O

def store_path(cfg: dict) -> Path:
    return Path(cfg["reviews_path"])


def append_record(cfg: dict, record: dict) -> None:
    """Append one record version atomically-enough for JSONL (single write,
    fsync): a crash leaves either the old or the new full line."""
    path = store_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def load_records(cfg: dict) -> dict[str, dict]:
    """All review records keyed by record_id; latest append wins."""
    path = store_path(cfg)
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue      # a torn trailing line from a crash is ignored;
                              # every earlier record stays loadable
            rid = rec.get("record_id")
            if rid:
                out[rid] = rec
    return out


# ---------------------------------------------------------------- records

def make_record(*, page_id: str, line_number: int, image_sha256: str,
                recognition_revision: int, bbox_xyxy, decision: str,
                verified_text: str | None, notes: str | None,
                model_text: str | None, reviewer: str,
                case_id: str = "", page_number: int = 0,
                record_id: str | None = None) -> dict:
    return {
        "schema": REVIEW_SCHEMA,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "record_id": record_id or f"rv-{uuid.uuid4().hex[:16]}",
        "page_id": page_id,
        "line_number": int(line_number),
        "image_sha256": image_sha256,
        "recognition_revision": int(recognition_revision),
        "bbox": list(bbox_xyxy),
        "bbox_convention": "xyxy-original-image-pixels",
        "decision": decision,
        "verified_text": verified_text,
        "notes": notes,
        "model_text": model_text,
        "reviewer": reviewer,
        "case_id": case_id,
        "page_number": int(page_number),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def record_from_db_row(row) -> dict:
    """Rebuild a durable record from a reviews-table row (migration)."""
    bbox = row["bbox"]
    if isinstance(bbox, str):
        try:
            bbox = json.loads(bbox)
        except json.JSONDecodeError:
            bbox = None
    if bbox is not None and len(bbox) == 4:
        # legacy rows stored xywh; canonical convention is xyxy
        conv = row["bbox_convention"] if "bbox_convention" in row.keys() else None
        if conv != "xyxy-original-image-pixels":
            bbox = xywh_to_xyxy(bbox)
    return {
        "schema": REVIEW_SCHEMA,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "record_id": row["record_id"] or synthetic_record_id(row),
        "page_id": row["page_id"],
        "line_number": int(row["line_number"]),
        "image_sha256": row["image_sha256"] or row["source_sha256"],
        "recognition_revision": row["recognition_revision"],
        "bbox": bbox,
        "bbox_convention": "xyxy-original-image-pixels",
        "decision": row["decision"],
        "verified_text": row["verified_text"],
        "notes": row["notes"],
        "model_text": row["model_text"],
        "reviewer": row["reviewer"],
        "case_id": row["case_id"],
        "page_number": row["page_number"],
        "updated_at": row["updated_at"],
    }


def synthetic_record_id(row) -> str:
    """Deterministic id for legacy rows created before record ids existed —
    stable across repeated ingests (idempotent migration)."""
    basis = "|".join(str(x) for x in (
        row["page_id"], row["line_number"], row["decision"],
        row["verified_text"] or "", row["updated_at"]))
    return "rv-legacy-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- matching

def _canon_bbox(bbox) -> list | None:
    if bbox is None:
        return None
    if isinstance(bbox, str):
        try:
            bbox = json.loads(bbox)
        except json.JSONDecodeError:
            return None
    if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
        return None
    return [float(v) for v in bbox]


def match_review_record(record: dict, *, page_sha: str | None,
                        revision: int | None,
                        line_map: dict[int, tuple[int, str]]) -> tuple:
    """Decide how a review record binds to the CURRENT recognition result.

    line_map: line_number -> (line_id, bbox_xyxy_json_string).
    Returns (line_id | None, match_state, match_reason | None).
    active only on full binding match — never on a different region,
    revision or image.
    """
    if page_sha is None:
        return None, "unresolved", "page_missing"
    rec_sha = record.get("image_sha256")
    if rec_sha and rec_sha != page_sha:
        return None, "unresolved", "image_changed"
    rec_rev = record.get("recognition_revision")
    if rec_rev is not None and revision is not None and int(rec_rev) != int(revision):
        return None, "unresolved", "revision_changed"
    entry = line_map.get(int(record.get("line_number", -1)))
    if entry is None:
        return None, "unresolved", "line_missing"
    line_id, line_bbox = entry
    if _canon_bbox(record.get("bbox")) != _canon_bbox(line_bbox):
        return None, "unresolved", "geometry_changed"
    return line_id, "active", None


# ---------------------------------------------------------------- DB bridge

def ingest_missing(cfg: dict, conn) -> int:
    """Ensure every review row present in the DB also exists in the durable
    store (protects rows written before the store existed).  Idempotent."""
    from htr_reviews import load_records, append_record
    have = set(load_records(cfg))
    n = 0
    rows = conn.execute("SELECT * FROM reviews").fetchall()
    for row in rows:
        rid = row["record_id"]
        if rid and rid in have:
            continue
        if not rid:
            rid = synthetic_record_id(row)
        rec = record_from_db_row(row)
        rec["record_id"] = rid
        append_record(cfg, rec)
        have.add(rid)
        n += 1
    return n


def save_review(cfg: dict, db_path, record: dict, *, line_id: int,
                match_state: str = "active",
                match_reason: str | None = None) -> None:
    """Durable-first review write: append to the JSONL store, then
    materialize into the DB (which stays a derived view)."""
    append_record(cfg, record)
    from htr_search.db import connect, apply_review_record
    conn = connect(db_path)
    try:
        apply_review_record(conn, record, line_id=line_id,
                            match_state=match_state,
                            match_reason=match_reason)
        conn.commit()
    finally:
        conn.close()
