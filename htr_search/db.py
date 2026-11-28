"""Index builder: portable sidecars -> SQLite + FTS5 (unified searchable DB).

The sidecar tree (htr_sidecar.py) is the portable source of truth for
recognition; the durable JSONL review store (htr_reviews.py) is the source
of truth for human reviews.  This module MATERIALIZES both into SQLite:

    python -m htr_search.db            # rebuild (also imports legacy pages)

Review integrity rules (P0):
  * a human review is NEVER deleted — recognition changes only flip its
    `match_state` between `active` (fully bound: image sha + recognition
    revision + line_number + bbox all match) and `unresolved` (explicit
    reason: page_missing / image_changed / revision_changed / line_missing /
    geometry_changed / superseded);
  * HUMAN-VERIFIED status is never transferred to a different spatial
    region, revision or image;
  * every DB review row corresponds to a durable record, so a complete
    database loss is recoverable from `reviews.jsonl` alone.

Diplomatic OCR text (text_raw) is never modified.  `lines.bbox` stays
[x, y, w, h] (historical server/UI consumers); sidecars store
[x_min, y_min, x_max, y_max] — conversion is tested.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

from .normalize import normalize

try:
    from htr_config import load as _load_config
    _CFG = _load_config()
    DB_PATH = Path(_CFG["db_path"])
    SIDECAR_ROOT = Path(_CFG["sidecar_root"])
    LEGACY_OCR_ROOT = Path(_CFG["legacy_ocr_root"])
    LEGACY_MANIFEST = Path(_CFG["legacy_manifest"])
    REVIEWS_PATH = Path(_CFG["reviews_path"])
except Exception:  # config unavailable (e.g. bare checkout) — historical defaults
    BENCH = Path(__file__).resolve().parent.parent
    DB_PATH = BENCH / "work" / "index" / "htr.db"
    SIDECAR_ROOT = BENCH / "work" / "indexer" / "sidecars"
    LEGACY_OCR_ROOT = BENCH / "work" / "index" / "ocr"
    LEGACY_MANIFEST = BENCH / "work" / "index_manifest.json"
    REVIEWS_PATH = BENCH / "work" / "indexer" / "reviews" / "reviews.jsonl"

SELECTION = DB_PATH.parent / ".." / "selection.json"  # AI-draft provisional refs
SELECTION = SELECTION.resolve()

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages(
  page_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL, case_short TEXT NOT NULL, page_number INTEGER NOT NULL,
  image_path TEXT NOT NULL, image_sha256 TEXT NOT NULL,
  width INTEGER, height INTEGER,
  status TEXT NOT NULL,
  line_count INTEGER, extra_line_count INTEGER, avg_confidence REAL,
  processed_at TEXT, provenance TEXT, error TEXT,
  collection TEXT, archive TEXT, fond TEXT, opis TEXT, delo TEXT,
  canonical_page_id TEXT
);
CREATE TABLE IF NOT EXISTS lines(
  line_id INTEGER PRIMARY KEY,
  page_id TEXT NOT NULL REFERENCES pages(page_id),
  line_number INTEGER NOT NULL,
  source TEXT NOT NULL CHECK(source IN ('blla-ocr','stage-b-gap')),
  text_raw TEXT NOT NULL,
  text_norm TEXT NOT NULL,
  bbox TEXT NOT NULL,
  confidence REAL,
  reading_order INTEGER NOT NULL,
  y_center REAL
);
CREATE INDEX IF NOT EXISTS idx_lines_page ON lines(page_id);
CREATE VIRTUAL TABLE IF NOT EXISTS lines_fts USING fts5(
  text_norm, content='lines', content_rowid='line_id', tokenize='unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS lines_trgm USING fts5(
  text_norm, content='lines', content_rowid='line_id', tokenize='trigram');
CREATE TABLE IF NOT EXISTS reviews(
  review_id INTEGER PRIMARY KEY,
  record_id TEXT UNIQUE,
  line_id INTEGER,
  page_id TEXT NOT NULL,
  line_number INTEGER NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('confirmed','rejected','uncertain')),
  verified_text TEXT,
  notes TEXT,
  source_sha256 TEXT NOT NULL,
  case_id TEXT NOT NULL, page_number INTEGER NOT NULL,
  bbox TEXT, model_text TEXT,
  reviewer TEXT NOT NULL DEFAULT 'local-human',
  updated_at TEXT NOT NULL,
  image_sha256 TEXT,
  recognition_revision INTEGER,
  match_state TEXT NOT NULL DEFAULT 'unresolved'
      CHECK(match_state IN ('active','unresolved')),
  match_reason TEXT,
  bbox_convention TEXT NOT NULL DEFAULT 'xyxy-original-image-pixels'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_reviews_active_line
  ON reviews(line_id) WHERE line_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_reviews_page ON reviews(page_id);
CREATE TABLE IF NOT EXISTS provisional_refs(
  page_id TEXT NOT NULL, line_number INTEGER NOT NULL,
  text TEXT, provenance TEXT, verified_flag INTEGER,
  PRIMARY KEY(page_id, line_number)
);
CREATE TABLE IF NOT EXISTS search_runs(
  run_id INTEGER PRIMARY KEY,
  query TEXT NOT NULL, profile TEXT, params TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS search_results(
  run_id INTEGER NOT NULL REFERENCES search_runs(run_id),
  rank INTEGER NOT NULL, line_id INTEGER NOT NULL,
  score REAL NOT NULL, components TEXT,
  PRIMARY KEY(run_id, rank)
);
"""

PAGE_EXTRA_COLS = ("collection", "archive", "fond", "opis", "delo",
                   "canonical_page_id")

NEW_REVIEWS_COLS = ("record_id", "line_id", "image_sha256",
                    "recognition_revision", "match_state", "match_reason",
                    "bbox_convention")


def connect(db_path: Path | str | None = None, readonly: bool = False) -> sqlite3.Connection:
    db_path = Path(db_path) if db_path else DB_PATH
    if readonly:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------- migration

def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pages)")}
    for col in PAGE_EXTRA_COLS:
        if col not in cols:
            conn.execute(f"ALTER TABLE pages ADD COLUMN {col} TEXT")
    # backfill from historical case_id conventions
    conn.execute(
        "UPDATE pages SET collection='cgamos-597', archive='ЦГА Москвы', "
        "fond='597', opis='1', delo=substr(case_short,1,3) "
        "WHERE collection IS NULL AND case_short GLOB '[0-9][0-9][0-9]'")
    conn.execute(
        "UPDATE pages SET collection='gavo', "
        "archive='ГАВО (Государственный архив Воронежской области)', "
        "fond='И331', opis='3', delo=substr(case_short,2) "
        "WHERE collection IS NULL AND case_short GLOB 'g[0-9]*'")

    rcols = {r[1] for r in conn.execute("PRAGMA table_info(reviews)")}
    if "match_state" not in rcols:
        _rebuild_reviews_table(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reviews)")}
    if "line_number" not in cols:      # ancient shape safety net
        conn.execute("ALTER TABLE reviews ADD COLUMN line_number INTEGER")
        conn.execute(
            "UPDATE reviews SET line_number="
            "(SELECT line_number FROM lines WHERE lines.line_id = reviews.line_id)")
    conn.commit()


def _rebuild_reviews_table(conn: sqlite3.Connection) -> None:
    """Legacy reviews (line_id NOT NULL, no binding columns) -> P0 schema.

    Every legacy row is preserved with a durable record id and its bindings
    derived from the current index; nothing is dropped.  Runs inside a
    transaction opened by the caller context (sqlite3 implicit txn)."""
    from htr_sidecar import xywh_to_xyxy
    from htr_reviews import synthetic_record_id
    legacy = [dict(r) for r in conn.execute("SELECT * FROM reviews")]
    conn.execute("ALTER TABLE reviews RENAME TO reviews_legacy")
    # indexes created by SCHEMA followed the rename — drop them by name
    # before recreating the fresh table (name collision otherwise)
    conn.execute("DROP INDEX IF EXISTS ux_reviews_active_line")
    conn.execute("DROP INDEX IF EXISTS idx_reviews_page")
    conn.execute("""
        CREATE TABLE reviews(
          review_id INTEGER PRIMARY KEY,
          record_id TEXT UNIQUE,
          line_id INTEGER,
          page_id TEXT NOT NULL,
          line_number INTEGER NOT NULL,
          decision TEXT NOT NULL CHECK(decision IN ('confirmed','rejected','uncertain')),
          verified_text TEXT,
          notes TEXT,
          source_sha256 TEXT NOT NULL,
          case_id TEXT NOT NULL, page_number INTEGER NOT NULL,
          bbox TEXT, model_text TEXT,
          reviewer TEXT NOT NULL DEFAULT 'local-human',
          updated_at TEXT NOT NULL,
          image_sha256 TEXT,
          recognition_revision INTEGER,
          match_state TEXT NOT NULL DEFAULT 'unresolved'
              CHECK(match_state IN ('active','unresolved')),
          match_reason TEXT,
          bbox_convention TEXT NOT NULL DEFAULT 'xyxy-original-image-pixels'
        )""")
    conn.execute(
        "CREATE UNIQUE INDEX ux_reviews_active_line ON reviews(line_id) "
        "WHERE line_id IS NOT NULL")
    for i, r in enumerate(legacy, start=1):
        bbox = r.get("bbox")
        if isinstance(bbox, str) and bbox:
            try:
                bbox = xywh_to_xyxy(json.loads(bbox))
            except (json.JSONDecodeError, ValueError, TypeError):
                bbox = None
        revision = conn.execute(
            "SELECT provenance FROM pages WHERE page_id=?",
            (r.get("page_id"),)).fetchone()
        rev = None
        if revision and revision["provenance"]:
            try:
                rev = json.loads(revision["provenance"]).get("revision")
            except json.JSONDecodeError:
                rev = None
        rid = r.get("record_id") or synthetic_record_id(r)
        line_id = r.get("line_id")
        conn.execute(
            "INSERT INTO reviews(review_id, record_id, line_id, page_id, "
            "line_number, decision, verified_text, notes, source_sha256, "
            "case_id, page_number, bbox, model_text, reviewer, updated_at, "
            "image_sha256, recognition_revision, match_state, match_reason, "
            "bbox_convention) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, rid, line_id, r["page_id"], r["line_number"], r["decision"],
             r.get("verified_text"), r.get("notes"), r["source_sha256"],
             r["case_id"], r["page_number"],
             json.dumps(bbox) if bbox is not None else None,
             r.get("model_text"), r.get("reviewer", "local-human"),
             r["updated_at"], r.get("image_sha256") or r["source_sha256"],
             rev, "active" if line_id else "unresolved",
             None if line_id else "legacy-unmatched",
             "xyxy-original-image-pixels"))
    conn.execute("DROP TABLE reviews_legacy")
    # re-verify every binding against the CURRENT index (sha/revision/geometry)
    _reverify_all_reviews(conn)


# ---------------------------------------------------------------- review view

def _page_binding(conn: sqlite3.Connection, page_id: str):
    """(image_sha256|None, recognition_revision|None, line_map) for a page."""
    page = conn.execute(
        "SELECT image_sha256, provenance FROM pages WHERE page_id=?",
        (page_id,)).fetchone()
    if page is None:
        return None, None, {}
    revision = None
    if page["provenance"]:
        try:
            revision = json.loads(page["provenance"]).get("revision")
        except json.JSONDecodeError:
            revision = None
    line_map: dict[int, tuple[int, str]] = {}
    for r in conn.execute(
            "SELECT line_id, line_number, bbox FROM lines WHERE page_id=?",
            (page_id,)):
        # DB stores xywh; binding convention is xyxy — convert once here
        x, y, w, h = json.loads(r["bbox"])
        line_map.setdefault(int(r["line_number"]),
                            (r["line_id"], json.dumps([x, y, x + w, y + h])))
    return page["image_sha256"], revision, line_map


def _verify_review_row(conn: sqlite3.Connection, row) -> tuple:
    """Current binding state of one review row (never destructive)."""
    from htr_reviews import match_review_record, record_from_db_row
    page_sha, revision, line_map = _page_binding(conn, row["page_id"])
    record = record_from_db_row(row)
    return match_review_record(record, page_sha=page_sha, revision=revision,
                               line_map=line_map)


def _apply_row_match(conn: sqlite3.Connection, row, result) -> tuple:
    line_id, state, reason = result
    changed = (row["line_id"] != line_id or row["match_state"] != state
               or row["match_reason"] != reason)
    if changed:
        conn.execute(
            "UPDATE reviews SET line_id=?, match_state=?, match_reason=? "
            "WHERE review_id=?", (line_id, state, reason, row["review_id"]))
    return state, changed


def _remap_reviews_for_page(conn: sqlite3.Connection, page_id: str) -> tuple:
    """Re-verify (never delete) every review of `page_id` against the freshly
    written page: active only on full binding match, otherwise unresolved
    with an explicit reason.  Returns (active, unresolved)."""
    active = unresolved = 0
    seen: set[int] = set()
    for row in conn.execute("SELECT * FROM reviews WHERE page_id=? "
                            "ORDER BY updated_at DESC, review_id DESC",
                            (page_id,)):
        line_id, state, reason = _verify_review_row(conn, row)
        if state == "active":
            if line_id in seen:          # one active review per line
                line_id, state, reason = None, "unresolved", "superseded"
            else:
                seen.add(line_id)
        _apply_row_match(conn, row, (line_id, state, reason))
        if state == "active":
            active += 1
        else:
            unresolved += 1
    return active, unresolved


def _reverify_all_reviews(conn: sqlite3.Connection) -> tuple:
    active = unresolved = 0
    seen: set[int] = set()
    for row in conn.execute(
            "SELECT * FROM reviews ORDER BY updated_at DESC, review_id DESC"):
        line_id, state, reason = _verify_review_row(conn, row)
        if state == "active":
            if line_id in seen:
                line_id, state, reason = None, "unresolved", "superseded"
            else:
                seen.add(line_id)
        _apply_row_match(conn, row, (line_id, state, reason))
        if state == "active":
            active += 1
        else:
            unresolved += 1
    return active, unresolved


def apply_review_record(conn: sqlite3.Connection, record: dict, *,
                        line_id: int | None, match_state: str,
                        match_reason: str | None = None) -> None:
    """Materialize one durable review record into the DB view."""
    rid = record["record_id"]
    existing = conn.execute("SELECT * FROM reviews WHERE record_id=?",
                            (rid,)).fetchone()
    if line_id is not None:
        # one active review per line: a displaced older binding is preserved
        # as unresolved/superseded, never deleted
        other = conn.execute(
            "SELECT * FROM reviews WHERE line_id=? AND record_id!=?",
            (line_id, rid)).fetchone()
        if other:
            conn.execute(
                "UPDATE reviews SET line_id=NULL, match_state='unresolved', "
                "match_reason='superseded' WHERE review_id=?",
                (other["review_id"],))
    vals = (
        record.get("page_id"), int(record.get("line_number", 0)),
        record["decision"], record.get("verified_text"),
        record.get("notes"), record.get("image_sha256"),
        record.get("case_id") or "unknown", int(record.get("page_number") or 0),
        json.dumps(record.get("bbox")) if record.get("bbox") is not None
        else None,
        record.get("model_text"), record.get("reviewer", "local-human"),
        record["updated_at"], record.get("image_sha256"),
        record.get("recognition_revision"), match_state, match_reason,
        record.get("bbox_convention", "xyxy-original-image-pixels"),
        line_id, rid)
    if existing:
        conn.execute(
            "UPDATE reviews SET page_id=?, line_number=?, decision=?, "
            "verified_text=?, notes=?, source_sha256=?, case_id=?, "
            "page_number=?, bbox=?, model_text=?, reviewer=?, updated_at=?, "
            "image_sha256=?, recognition_revision=?, match_state=?, "
            "match_reason=?, bbox_convention=?, line_id=? "
            "WHERE record_id=?", vals)
    else:
        conn.execute(
            "INSERT INTO reviews(page_id, line_number, decision, verified_text,"
            " notes, source_sha256, case_id, page_number, bbox, model_text,"
            " reviewer, updated_at, image_sha256, recognition_revision,"
            " match_state, match_reason, bbox_convention, line_id, record_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)


def _fts_delete(conn: sqlite3.Connection, line_ids: list[int]) -> None:
    """Remove rows from the external-content FTS indexes (old values required)."""
    if not line_ids:
        return
    for i in range(0, len(line_ids), 900):
        chunk = line_ids[i:i + 900]
        ph = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT line_id, text_norm FROM lines WHERE line_id IN ({ph})",
            chunk).fetchall()
        for r in rows:
            conn.execute(
                "INSERT INTO lines_fts(lines_fts, rowid, text_norm) "
                "VALUES('delete', ?, ?)", (r["line_id"], r["text_norm"]))
            conn.execute(
                "INSERT INTO lines_trgm(lines_trgm, rowid, text_norm) "
                "VALUES('delete', ?, ?)", (r["line_id"], r["text_norm"]))


# ---------------------------------------------------------------- upsert

def upsert_page(db_path: Path | str | None, sidecar: dict) -> dict:
    """Transactionally index one sidecar (idempotent per page_id).

    Order inside one transaction: FTS-delete old lines -> delete lines ->
    insert page+lines -> FTS-insert -> re-verify this page's reviews (never
    deleting any).  A crash rolls the whole page back; search stays
    available (WAL).
    """
    conn = connect(db_path)
    stats = {"page_id": sidecar["source"]["page_id"], "lines": 0,
             "reviews_active": 0, "reviews_unresolved": 0}
    try:
        conn.executescript(SCHEMA)   # safe on fresh DBs (IF NOT EXISTS)
        _migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        pid = sidecar["source"]["page_id"]
        rec = sidecar["recognition"]
        status = rec.get("status", "ok")
        old_ids = [r["line_id"] for r in conn.execute(
            "SELECT line_id FROM lines WHERE page_id=?", (pid,))]
        _fts_delete(conn, old_ids)
        conn.execute("DELETE FROM lines WHERE page_id=?", (pid,))

        src = sidecar["source"]
        img = sidecar["image"]
        lines = sidecar["lines"] if status == "ok" else []
        confs = [ln["confidence"] for ln in lines
                 if ln.get("confidence") is not None]
        provenance = {k: rec.get(k) for k in (
            "engine", "model", "model_sha256", "segmentation",
            "segmentation_sha256", "patch", "device", "origin",
            "config_fingerprint", "revision", "import_validation",
            "reused_from_benchmark") if rec.get(k) is not None}
        dup_of = sidecar.get("content_group", {}).get("duplicate_of")
        if dup_of:
            provenance["duplicate_of"] = dup_of
        conn.execute(
            "INSERT OR REPLACE INTO pages(page_id, case_id, case_short, "
            "page_number, image_path, image_sha256, width, height, status, "
            "line_count, extra_line_count, avg_confidence, processed_at, "
            "provenance, error, collection, archive, fond, opis, delo, "
            "canonical_page_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, src.get("case_ref") or src.get("source_ref") or "unknown",
             src.get("case_short") or pid, int(src.get("page_number") or 0),
             src.get("image_path") or "", src.get("sha256") or "",
             img.get("width"), img.get("height"), status,
             len(lines), sum(1 for ln in lines
                             if ln.get("source") == "stage-b-gap"),
             (sum(confs) / len(confs)) if confs else None,
             rec.get("recognized_at"), json.dumps(provenance,
                                                  ensure_ascii=False),
             rec.get("error"), src.get("collection"), src.get("archive"),
             src.get("fond"), src.get("opis"), src.get("delo"),
             dup_of if status == "duplicate" else None))

        for i, ln in enumerate(lines, start=1):
            x0, y0, x1, y1 = ln["bbox"]          # sidecar: xyxy
            bbox_xywh = json.dumps([x0, y0, x1 - x0, y1 - y0])
            conn.execute(
                "INSERT INTO lines(page_id, line_number, source, text_raw, "
                "text_norm, bbox, confidence, reading_order, y_center) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (pid, int(ln.get("line_number") or i),
                 ln.get("source", "blla-ocr"), ln["text_raw"],
                 ln.get("text_norm") or normalize(ln["text_raw"]),
                 bbox_xywh, ln.get("confidence"),
                 int(ln.get("reading_order") or i),
                 y0 + (y1 - y0) / 2.0))
            row = conn.execute(
                "SELECT line_id FROM lines WHERE page_id=? AND line_number=?",
                (pid, ln.get("line_number") or i)).fetchone()
            conn.execute(
                "INSERT INTO lines_fts(rowid, text_norm) VALUES(?,?)",
                (row["line_id"], ln.get("text_norm") or
                 normalize(ln["text_raw"])))
            conn.execute(
                "INSERT INTO lines_trgm(rowid, text_norm) VALUES(?,?)",
                (row["line_id"], ln.get("text_norm") or
                 normalize(ln["text_raw"])))

        stats["lines"] = len(lines)
        stats["reviews_active"], stats["reviews_unresolved"] = \
            _remap_reviews_for_page(conn, pid)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return stats


# ---------------------------------------------------------------- build

def iter_sidecars(sidecar_root: Path | None = None):
    root = Path(sidecar_root) if sidecar_root else SIDECAR_ROOT
    if not root.exists():
        return
    for p in sorted(root.rglob("*.htr.json")):
        if "revisions" in p.parts:
            continue
        yield p


def _seed_reviews_from_store(conn, reviews_path: Path) -> dict:
    """Materialize every durable review record into the view (idempotent).

    Records whose page/line no longer matches stay in the table as
    unresolved rows — never dropped.  Returns the record dict."""
    from htr_reviews import load_records
    cfg = {"reviews_path": str(reviews_path)}
    records = load_records(cfg)
    for rec in records.values():
        row = conn.execute("SELECT review_id FROM reviews WHERE record_id=?",
                           (rec.get("record_id"),)).fetchone()
        if row:
            continue
        conn.execute(
            "INSERT INTO reviews(record_id, line_id, page_id, line_number, "
            "decision, verified_text, notes, source_sha256, case_id, "
            "page_number, bbox, model_text, reviewer, updated_at, "
            "image_sha256, recognition_revision, match_state, match_reason, "
            "bbox_convention) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.get("record_id"), None, rec.get("page_id", ""),
             int(rec.get("line_number") or 0), rec["decision"],
             rec.get("verified_text"), rec.get("notes"),
             rec.get("image_sha256") or "", rec.get("case_id") or "unknown",
             int(rec.get("page_number") or 0),
             json.dumps(rec.get("bbox")) if rec.get("bbox") is not None
             else None, rec.get("model_text"),
             rec.get("reviewer", "local-human"), rec["updated_at"],
             rec.get("image_sha256"), rec.get("recognition_revision"),
             "unresolved", "pending_match",
             rec.get("bbox_convention", "xyxy-original-image-pixels")))
    return records


def build(db_path: Path | str | None = None,
          sidecar_root: Path | str | None = None,
          import_legacy: bool = True,
          reviews_path: Path | str | None = None) -> dict:
    """Full rebuild from portable sidecars + the durable review store.

    Safe on a brand-new empty database: reviews are seeded from
    `reviews.jsonl`, pages/lines from sidecars.  Deterministic for fixed
    inputs.  Preserves every review (active or unresolved)."""
    db_path = Path(db_path) if db_path else DB_PATH
    if reviews_path is None:
        reviews_path = REVIEWS_PATH
    reviews_path = Path(reviews_path)
    if import_legacy:
        try:
            from htr_import import legacy_to_sidecars
            legacy_to_sidecars()
        except ImportError:
            pass  # htr_import not available: sidecars must already exist
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    # AI-draft provisional references (selection.json) — kept strictly separate
    if SELECTION.exists():
        conn.execute("DELETE FROM provisional_refs")
        for r in json.loads(SELECTION.read_text(encoding="utf-8")):
            conn.execute(
                "INSERT OR REPLACE INTO provisional_refs VALUES(?,?,?,?,?)",
                (r["page"], r["line_number"], r.get("provisional_reference", ""),
                 r.get("reference_provenance", "none"),
                 1 if r.get("verified") else 0))
    # P0: the durable store must contain every review the DB knows about
    # before any rebuild touches the view (recovery from DB loss works too:
    # an empty DB simply seeds from the store).
    from htr_reviews import ingest_missing
    ingested = ingest_missing({"reviews_path": str(reviews_path)}, conn)
    records = _seed_reviews_from_store(conn, reviews_path)
    conn.commit()
    conn.close()

    pages_ok = pages_failed = pages_dup = lines_total = 0
    reviews_active = reviews_unresolved = 0
    invalid: list[str] = []
    present_pids: set[str] = set()      # pages with a sidecar file (any state)
    for sc_path in iter_sidecars(sidecar_root):
        try:
            sidecar = json.loads(sc_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            invalid.append(str(sc_path))
            continue
        from htr_sidecar import validate_sidecar
        present_pids.add(sidecar.get("source", {}).get("page_id", ""))
        problems = validate_sidecar(sidecar)
        if problems:
            invalid.append(f"{sc_path}: {problems[:3]}")
            continue
        st = upsert_page(db_path, sidecar)
        reviews_unresolved += st["reviews_unresolved"]
        status = sidecar["recognition"].get("status", "ok")
        if status == "ok":
            pages_ok += 1
            lines_total += len(sidecar["lines"])
        elif status == "duplicate":
            pages_dup += 1
        else:
            pages_failed += 1

    # reviews whose page never made it into the index stay visible, flagged
    conn = connect(db_path)
    # stale pages: rows whose sidecar no longer exists are pruned (a clean
    # rebuild must not retain pages absent from the portable artifacts).
    # Their reviews are NEVER deleted — they fall to page_missing below.
    stale = [r["page_id"] for r in conn.execute(
        "SELECT page_id FROM pages") if r["page_id"] not in present_pids]
    pruned = 0
    for pid in stale:
        old_ids = [r["line_id"] for r in conn.execute(
            "SELECT line_id FROM lines WHERE page_id=?", (pid,))]
        _fts_delete(conn, old_ids)
        conn.execute("DELETE FROM lines WHERE page_id=?", (pid,))
        conn.execute("DELETE FROM pages WHERE page_id=?", (pid,))
        pruned += 1
    conn.execute(
        "UPDATE reviews SET match_reason='page_missing' "
        "WHERE match_state='unresolved' AND match_reason='pending_match' "
        "AND page_id NOT IN (SELECT page_id FROM pages)")
    # invariant: an ACTIVE review always points at a live line of a live
    # page; anything orphaned by pruning becomes unresolved (never deleted)
    conn.execute(
        "UPDATE reviews SET line_id=NULL, match_state='unresolved', "
        "match_reason='page_missing' WHERE match_state='active' AND "
        "page_id NOT IN (SELECT page_id FROM pages)")
    conn.execute(
        "UPDATE reviews SET line_id=NULL, match_state='unresolved', "
        "match_reason='line_missing' WHERE match_state='active' AND "
        "line_id NOT IN (SELECT line_id FROM lines)")
    conn.execute("INSERT INTO lines_fts(lines_fts) VALUES('rebuild')")
    conn.execute("INSERT INTO lines_trgm(lines_trgm) VALUES('rebuild')")
    conn.commit()
    counts = {r[0]: r[1] for r in conn.execute(
        "SELECT match_state, COUNT(*) FROM reviews GROUP BY match_state")}
    reviews_active = counts.get("active", 0)
    reviews_unresolved = counts.get("unresolved", 0)
    have = {r["page_id"] for r in conn.execute("SELECT page_id FROM pages")}
    orphans = conn.execute(
        "SELECT COUNT(*) FROM lines l LEFT JOIN pages p USING(page_id) "
        "WHERE p.page_id IS NULL").fetchone()[0]
    fk_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
    return {"pages_ok": pages_ok, "pages_failed": pages_failed,
            "pages_duplicate": pages_dup, "lines": lines_total,
            "reviews_total": reviews_active + reviews_unresolved,
            "reviews_active": reviews_active,
            "reviews_unresolved": reviews_unresolved,
            "reviews_ingested_from_db": ingested,
            "review_records_in_store": len(records),
            "orphan_lines": orphans,
            "foreign_key_issues": len(fk_issues),
            "pages_pruned": pruned,
            "invalid_sidecars": invalid,
            "total_pages_in_db": len(have)}


def main() -> int:
    t0 = time.time()
    stats = build()
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print(f"index built in {time.time() - t0:.1f}s -> {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
