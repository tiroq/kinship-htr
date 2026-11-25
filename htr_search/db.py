"""Index builder: portable sidecars -> SQLite + FTS5 (unified searchable DB).

The sidecar tree (htr_sidecar.py) is the portable source of truth; this
module can rebuild the whole database from it without rerunning HTR:

    python -m htr_search.db            # rebuild (also imports legacy pages)

Legacy per-page artifacts (work/index/ocr/<page>/, pre-dating sidecars) are
converted to sidecars first (htr_import.legacy_to_sidecars), so one command
covers the full corpus.

Diplomatic OCR text (text_raw) is never modified.  Normalized copies exist
only in lines.text_norm and the FTS5 indexes.  Human review decisions live
in `reviews` and are remapped by their stable (page_id, line_number) key on
every rebuild/upsert — never discarded.  AI-draft provisional references
live in `provisional_refs` and are NEVER promoted to human ground truth.

`lines.bbox` stays [x, y, w, h] (historical server/UI consumers); sidecars
store [x_min, y_min, x_max, y_max] — conversion is tested.
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
except Exception:  # config unavailable (e.g. bare checkout) — historical defaults
    BENCH = Path(__file__).resolve().parent.parent
    DB_PATH = BENCH / "work" / "index" / "htr.db"
    SIDECAR_ROOT = BENCH / "work" / "indexer" / "sidecars"
    LEGACY_OCR_ROOT = BENCH / "work" / "index" / "ocr"
    LEGACY_MANIFEST = BENCH / "work" / "index_manifest.json"

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
  UNIQUE(case_short, page_number)
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
  line_id INTEGER UNIQUE NOT NULL REFERENCES lines(line_id),
  page_id TEXT NOT NULL,
  line_number INTEGER NOT NULL DEFAULT 0,
  decision TEXT NOT NULL CHECK(decision IN ('confirmed','rejected','uncertain')),
  verified_text TEXT,
  notes TEXT,
  source_sha256 TEXT NOT NULL,
  case_id TEXT NOT NULL, page_number INTEGER NOT NULL,
  bbox TEXT, model_text TEXT,
  reviewer TEXT NOT NULL DEFAULT 'local-human',
  updated_at TEXT NOT NULL
);
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

PAGE_EXTRA_COLS = ("collection", "archive", "fond", "opis", "delo")


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
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reviews)")}
    if "line_number" not in cols:
        conn.execute("ALTER TABLE reviews ADD COLUMN line_number INTEGER")
        conn.execute(
            "UPDATE reviews SET line_number="
            "(SELECT line_number FROM lines WHERE lines.line_id = reviews.line_id)")
    conn.commit()


# ---------------------------------------------------------------- helpers

def _legacy_dims() -> dict:
    """page_id -> (width, height) from the historical index manifest."""
    out = {}
    try:
        for pid, (_p, _sha, w, h) in json.loads(
                LEGACY_MANIFEST.read_text(encoding="utf-8")).items():
            out[pid] = (w, h)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return out


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


def _remap_reviews_for_page(conn: sqlite3.Connection, page_id: str) -> tuple[int, int]:
    """Re-attach reviews of `page_id` to the current line_ids via the stable
    (page_id, line_number) key.  Returns (remapped, dropped)."""
    reviews = list(conn.execute("SELECT * FROM reviews WHERE page_id=?",
                                (page_id,)))
    remapped = dropped = 0
    for r in reviews:
        row = conn.execute(
            "SELECT line_id FROM lines WHERE page_id=? AND line_number=?",
            (page_id, r["line_number"])).fetchone()
        if row and row["line_id"] != r["line_id"]:
            conn.execute("UPDATE reviews SET line_id=? WHERE review_id=?",
                         (row["line_id"], r["review_id"]))
            remapped += 1
        elif row is None:
            # keep the review row orphan-free: park it is not allowed by the
            # UNIQUE(FK) constraint semantics — drop and count honestly
            conn.execute("DELETE FROM reviews WHERE review_id=?", (r["review_id"],))
            dropped += 1
    return remapped, dropped


def upsert_page(db_path: Path | str | None, sidecar: dict) -> dict:
    """Transactionally index one sidecar (idempotent per page_id).

    Order inside one transaction: capture reviews -> FTS-delete old lines ->
    delete lines/pages -> insert page+lines -> FTS-insert -> remap reviews.
    A crash rolls the whole page back; search stays available (WAL).
    """
    conn = connect(db_path)
    stats = {"page_id": sidecar["source"]["page_id"], "lines": 0,
             "reviews_remapped": 0, "reviews_dropped": 0}
    try:
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
            "provenance, error, collection, archive, fond, opis, delo) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
             src.get("fond"), src.get("opis"), src.get("delo")))

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
        stats["reviews_remapped"], stats["reviews_dropped"] = \
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


def build(db_path: Path | str | None = None,
          sidecar_root: Path | str | None = None,
          import_legacy: bool = True) -> dict:
    """Full rebuild from portable sidecars (optionally importing legacy
    artifacts first).  Preserves reviews via (page_id, line_number) remap."""
    db_path = Path(db_path) if db_path else DB_PATH
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
    conn.close()

    pages_ok = pages_failed = pages_dup = lines_total = 0
    reviews_remapped = reviews_dropped = 0
    invalid: list[str] = []
    for sc_path in iter_sidecars(sidecar_root):
        try:
            sidecar = json.loads(sc_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            invalid.append(str(sc_path))
            continue
        from htr_sidecar import validate_sidecar
        problems = validate_sidecar(sidecar)
        if problems:
            invalid.append(f"{sc_path}: {problems[:3]}")
            continue
        st = upsert_page(db_path, sidecar)
        reviews_remapped += st["reviews_remapped"]
        reviews_dropped += st["reviews_dropped"]
        status = sidecar["recognition"].get("status", "ok")
        if status == "ok":
            pages_ok += 1
            lines_total += len(sidecar["lines"])
        elif status == "duplicate":
            pages_dup += 1
        else:
            pages_failed += 1

    conn = connect(db_path)
    conn.execute("INSERT INTO lines_fts(lines_fts) VALUES('rebuild')")
    conn.execute("INSERT INTO lines_trgm(lines_trgm) VALUES('rebuild')")
    conn.commit()
    have = {r["page_id"] for r in conn.execute("SELECT page_id FROM pages")}
    conn.close()
    return {"pages_ok": pages_ok, "pages_failed": pages_failed,
            "pages_duplicate": pages_dup, "lines": lines_total,
            "reviews_remapped": reviews_remapped,
            "reviews_dropped": reviews_dropped,
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
