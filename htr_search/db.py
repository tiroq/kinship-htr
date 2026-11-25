"""Index builder: OCR per-page outputs -> SQLite + FTS5 (work/index/htr.db).

Diplomatic OCR text (text_raw) is never modified.  Normalized copies exist
only in lines.text_norm and the FTS5 indexes.  Human review decisions live in
`reviews`; AI-draft provisional references live in `provisional_refs` and are
NEVER promoted to human ground truth (no code path writes provisional_refs
content into reviews; verified_text comes exclusively from review POSTs).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

from .normalize import normalize

BENCH = Path("/Users/mysterx/dev/kinship-htr-bench")
OCR_ROOT = BENCH / "work" / "index" / "ocr"
DB_PATH = BENCH / "work" / "index" / "htr.db"
SELECTION = BENCH / "work" / "selection.json"          # AI-draft provisional refs

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages(
  page_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL, case_short TEXT NOT NULL, page_number INTEGER NOT NULL,
  image_path TEXT NOT NULL, image_sha256 TEXT NOT NULL,
  width INTEGER, height INTEGER,
  status TEXT NOT NULL,
  line_count INTEGER, extra_line_count INTEGER, avg_confidence REAL,
  processed_at TEXT, provenance TEXT, error TEXT,
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


def connect(db_path: Path = DB_PATH, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def build(db_path: Path = DB_PATH, ocr_root: Path = OCR_ROOT) -> dict:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    # migrate: reviews.line_number was missing (remap-by-(page_id,line_number)
    # expected it but never persisted it); backfill from lines BEFORE deletion
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reviews)")}
    if "line_number" not in cols:
        conn.execute("ALTER TABLE reviews ADD COLUMN line_number INTEGER")
        conn.execute(
            "UPDATE reviews SET line_number="
            "(SELECT line_number FROM lines WHERE lines.line_id = reviews.line_id)")
        conn.commit()
    # page dimensions come from the manifest (per-page meta.json has no dims)
    manifest_dims = {}
    for pid, (_p, _sha, w, h) in json.loads(
            (BENCH / "work" / "index_manifest.json").read_text()).items():
        manifest_dims[pid] = (w, h)
    # keep human reviews across the rebuild: lines get new line_ids, so remap
    # review rows by their stable (page_id, line_number) key
    old_reviews = list(conn.execute("SELECT * FROM reviews"))
    conn.execute("DELETE FROM lines")
    conn.execute("DELETE FROM pages")

    pages_ok = pages_failed = lines_total = extra_total = 0
    for pdir in sorted(ocr_root.glob("*/")):
        pid = pdir.name
        if pid.startswith("_tmp"):
            continue
        meta_p = pdir / "meta.json"
        if not meta_p.exists():
            continue  # page not yet processed (or interrupted) — counted absent
        meta = json.loads(meta_p.read_text())
        short = pid.split("-")[0]
        tjson = pdir / "transcriptions" / f"{pid}.json"
        page_lines = []
        if meta.get("status") == "ok" and tjson.exists():
            tj = json.loads(tjson.read_text())
            for ln in tj.get("lines", []):
                x, y, w, h = ln["bbox"]
                page_lines.append((ln["line_number"], "blla-ocr", ln.get("text", ""),
                                   json.dumps(ln["bbox"]), ln.get("confidence"),
                                   y + h / 2.0))
        extra = []
        sb_p = pdir / "stage_b.json"
        if sb_p.exists():
            for ln in json.loads(sb_p.read_text()).get("lines", []):
                x, y, w, h = ln["bbox"]
                extra.append((ln["line_number"], "stage-b-gap", ln.get("text", ""),
                              json.dumps(ln["bbox"]), ln.get("confidence"),
                              y + h / 2.0))

        if meta.get("status") == "ok":
            pages_ok += 1
        elif meta.get("status") == "failed":
            pages_failed += 1
        lines_total += len(page_lines)
        extra_total += len(extra)
        w_dim, h_dim = manifest_dims.get(pid, (meta.get("width"), meta.get("height")))
        conn.execute(
            "INSERT OR REPLACE INTO pages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, meta["case_id"], short, meta["page_number"],
             meta["image_path"], meta["image_sha256"], w_dim, h_dim,
             meta.get("status", "unknown"),
             len(page_lines), len(extra), meta.get("avg_confidence"),
             meta.get("completed_at"),
             json.dumps({k: meta[k] for k in (
                 "engine", "model", "model_sha256", "segmentation",
                 "patch", "device", "reused_from_benchmark") if k in meta},
                 ensure_ascii=False),
             meta.get("error")))

        order = 0
        for num, src, text, bbox, conf, yc in page_lines + extra:
            order += 1
            conn.execute(
                "INSERT INTO lines(page_id,line_number,source,text_raw,text_norm,"
                "bbox,confidence,reading_order,y_center) VALUES(?,?,?,?,?,?,?,?,?)",
                (pid, num, src, text, normalize(text), bbox, conf, order, yc))

    # AI-draft provisional references (selection.json) — kept strictly separate
    if SELECTION.exists():
        conn.execute("DELETE FROM provisional_refs")
        for r in json.loads(SELECTION.read_text()):
            conn.execute(
                "INSERT OR REPLACE INTO provisional_refs VALUES(?,?,?,?,?)",
                (r["page"], r["line_number"], r.get("provisional_reference", ""),
                 r.get("reference_provenance", "none"),
                 1 if r.get("verified") else 0))

    # remap surviving reviews onto the new line_ids (page_id + line_number)
    conn.execute("DELETE FROM reviews")
    remapped = dropped = 0
    for r in old_reviews:
        row = conn.execute(
            "SELECT line_id FROM lines WHERE page_id=? AND line_number=?",
            (r["page_id"], r["line_number"])).fetchone()
        if row:
            conn.execute(
                "INSERT INTO reviews(line_id, page_id, line_number, decision, verified_text,"
                " notes, source_sha256, case_id, page_number, bbox, model_text,"
                " reviewer, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["line_id"], r["page_id"], r["line_number"], r["decision"],
                 r["verified_text"], r["notes"], r["source_sha256"],
                 r["case_id"], r["page_number"], r["bbox"], r["model_text"],
                 r["reviewer"], r["updated_at"]))
        else:
            dropped += 1

    conn.execute("INSERT INTO lines_fts(lines_fts) VALUES('rebuild')")
    conn.execute("INSERT INTO lines_trgm(lines_trgm) VALUES('rebuild')")
    conn.commit()

    # manifest coverage audit: pages expected vs present
    manifest = json.loads((BENCH / "work" / "index_manifest.json").read_text())
    have = {r["page_id"] for r in conn.execute("SELECT page_id FROM pages")}
    conn.close()
    missing = sorted(set(manifest) - have)
    return {"pages_ok": pages_ok, "pages_failed": pages_failed,
            "lines_ocr": lines_total, "lines_extra": extra_total,
            "reviews_remapped": remapped, "reviews_dropped": dropped,
            "missing_from_index": missing}


def main() -> int:
    t0 = time.time()
    stats = build()
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print(f"index built in {time.time() - t0:.1f}s -> {DB_PATH}")
    if stats["missing_from_index"]:
        print(f"WARNING: {len(stats['missing_from_index'])} pages not yet in index")
    return 0


if __name__ == "__main__":
    sys.exit(main())
