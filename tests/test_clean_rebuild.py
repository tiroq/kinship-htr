#!/usr/bin/env python3
"""P1 — clean rebuild into a NEW, EMPTY SQLite database.

Not in-place reindexing: every build here starts from a nonexistent DB file
and must reconstruct the full index from portable sidecars + the durable
review store alone.  Verifies:

  * identical page/line coverage vs the sidecar corpus;
  * human reviews preserved from durable artifacts (incl. a record whose
    page has no sidecar -> unresolved/page_missing);
  * duplicate-source resolution (direct canonical, no dup->dup rows);
  * no stale pages / orphaned lines / foreign-key issues;
  * valid FTS5 indexes (counts + queries + integrity-check);
  * deterministic reconstruction (two builds -> byte-identical dumps);
  * correct recognition revision identifiers (sidecar line ids, DB
    provenance revision).

Run:  htrbench-python tests/test_clean_rebuild.py   (pytest-compatible)
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from htr_reviews import append_record, load_records, make_record  # noqa: E402
from tests._fixtures import add_review, build_test_db, make_sidecars  # noqa: E402

FAIL: list[str] = []


def check(cond, msg):
    if cond:
        print(f"PASS {msg}")
    else:
        FAIL.append(msg)
        print(f"FAIL {msg}")


def dump_db(db_path: Path) -> str:
    """Canonical content dump of pages + lines + reviews (order-stable)."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    h = hashlib.sha256()
    for table, cols in (
            ("pages", "page_id, case_id, case_short, page_number, image_path,"
                      " image_sha256, width, height, status, line_count,"
                      " extra_line_count, avg_confidence, processed_at,"
                      " provenance, error, collection, archive, fond, opis,"
                      " delo, canonical_page_id"),
            ("lines", "line_id, page_id, line_number, source, text_raw,"
                      " text_norm, bbox, confidence, reading_order, y_center"),
            ("reviews", "record_id, line_id, page_id, line_number, decision,"
                        " verified_text, notes, source_sha256, case_id,"
                        " page_number, bbox, model_text, reviewer, updated_at,"
                        " image_sha256, recognition_revision, match_state,"
                        " match_reason, bbox_convention")):
        rows = conn.execute(
            f"SELECT {cols} FROM {table} ORDER BY 1").fetchall()
        payload = json.dumps([list(r) for r in rows], ensure_ascii=False,
                             sort_keys=True, default=str)
        h.update(table.encode())
        h.update(payload.encode("utf-8"))
    conn.close()
    return h.hexdigest()


def main() -> int:
    with tempfile.TemporaryDirectory() as tds:
        td = Path(tds)
        sidecar_root = td / "sidecars"
        sidecar_root.mkdir()
        base_db = td / "base.db"
        reviews = td / "reviews.jsonl"
        make_sidecars(sidecar_root)

        # 1) seed the durable store through the production path
        stats0 = build_test_db(base_db, sidecar_root, reviews_path=reviews)
        check(stats0["pages_ok"] == 2 and stats0["lines"] == 5,
              f"seed build: {stats0['pages_ok']}p/{stats0['lines']}l")
        sc = json.loads((sidecar_root / "g999-p000.htr.json").read_text())
        record = add_review(base_db, sc, 1, decision="confirmed",
                            verified_text="Той же вотчины деревни Волченокъ",
                            reviews_path=reviews)
        # a durable record whose page has NO sidecar (historical review of
        # a page that was never imported)
        ghost = make_record(page_id="ghost-p001", line_number=7,
                            image_sha256="ee" * 32, recognition_revision=3,
                            bbox_xyxy=[5, 5, 50, 25], decision="confirmed",
                            verified_text="строка из утраченной страницы",
                            notes="page no longer in the corpus",
                            model_text="машина", reviewer="local-human",
                            case_id="lost:case", page_number=1)
        append_record({"reviews_path": str(reviews)}, ghost)
        check(len(load_records({"reviews_path": str(reviews)})) == 2,
              "durable store holds both review records")

        # 2) CLEAN rebuild #1 — brand-new empty database
        new1 = td / "new1.db"
        stats1 = build_test_db(new1, sidecar_root, reviews_path=reviews)
        check(stats1["pages_ok"] == 2 and stats1["pages_failed"] == 1 and
              stats1["pages_duplicate"] == 1 and stats1["lines"] == 5 and
              stats1["total_pages_in_db"] == 4,
              f"clean build coverage: {stats1['pages_ok']}ok/"
              f"{stats1['pages_failed']}fail/{stats1['pages_duplicate']}dup "
              f"/{stats1['lines']} lines")
        check(stats1["reviews_total"] == 2 and stats1["reviews_active"] == 1 and
              stats1["reviews_unresolved"] == 1,
              f"clean build reviews from store: "
              f"{stats1['reviews_active']} active + "
              f"{stats1['reviews_unresolved']} unresolved")
        check(stats1["orphan_lines"] == 0 and
              stats1["foreign_key_issues"] == 0,
              f"no orphan lines ({stats1['orphan_lines']}), no FK issues "
              f"({stats1['foreign_key_issues']})")
        check(stats1["invalid_sidecars"] == [],
              "no invalid sidecars")

        import sqlite3
        conn = sqlite3.connect(new1)
        conn.row_factory = sqlite3.Row
        # reviews
        r = conn.execute("SELECT * FROM reviews WHERE record_id=?",
                         (record["record_id"],)).fetchone()
        check(r is not None and r["match_state"] == "active" and
              r["line_id"] is not None and
              r["image_sha256"] == sc["source"]["sha256"] and
              r["recognition_revision"] == 1 and
              r["verified_text"].startswith("Той же"),
              "review recovered active with full binding (sha+revision+text)")
        g = conn.execute("SELECT * FROM reviews WHERE record_id=?",
                         (ghost["record_id"],)).fetchone()
        check(g is not None and g["match_state"] == "unresolved" and
              g["match_reason"] == "page_missing" and g["line_id"] is None,
              f"orphan-page review preserved as unresolved "
              f"({g and g['match_reason']})")
        # duplicate resolution: direct canonical, valid target
        d = conn.execute("SELECT status, canonical_page_id FROM pages "
                         "WHERE page_id='g997-p000'").fetchone()
        c = conn.execute("SELECT status, line_count FROM pages "
                         "WHERE page_id='g999-p000'").fetchone()
        check(d["status"] == "duplicate" and
              d["canonical_page_id"] == "g999-p000" and
              c["status"] == "ok" and (c["line_count"] or 0) > 0,
              "duplicate resolves directly to a valid ok canonical")
        bad_dup = conn.execute(
            "SELECT COUNT(*) FROM pages p WHERE p.status='duplicate' AND "
            "(p.canonical_page_id IS NULL OR p.canonical_page_id='' OR "
            "p.canonical_page_id=p.page_id OR EXISTS("
            "SELECT 1 FROM pages q WHERE q.page_id=p.canonical_page_id AND "
            "q.status!='ok'))").fetchone()[0]
        check(bad_dup == 0, f"no dangling/invalid duplicate rows ({bad_dup})")
        # no stale pages: every page comes from a current sidecar
        sidecar_pages = set()
        for p in sidecar_root.rglob("*.htr.json"):
            if "revisions" in p.parts:
                continue
            sidecar_pages.add(json.loads(p.read_text())["source"]["page_id"])
        db_pages = {row["page_id"] for row in
                    conn.execute("SELECT page_id FROM pages")}
        check(db_pages == sidecar_pages,
              f"page set == sidecar set (extra={db_pages - sidecar_pages}, "
              f"missing={sidecar_pages - db_pages})")
        # orphan lines
        orphans = conn.execute(
            "SELECT COUNT(*) FROM lines l LEFT JOIN pages p USING(page_id) "
            "WHERE p.page_id IS NULL").fetchone()[0]
        check(orphans == 0, f"orphaned lines: {orphans}")
        # FTS5 validity
        fts_n = conn.execute("SELECT COUNT(*) FROM lines_fts").fetchone()[0]
        trgm_n = conn.execute("SELECT COUNT(*) FROM lines_trgm").fetchone()[0]
        lines_n = conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
        check(fts_n == lines_n and trgm_n == lines_n,
              f"FTS row counts match lines ({fts_n}/{trgm_n}/{lines_n})")
        hits = conn.execute(
            "SELECT rowid FROM lines_fts WHERE lines_fts MATCH 'волченок'"
        ).fetchall()
        check(len(hits) >= 1, "FTS5 MATCH query works after clean build")
        try:
            conn.execute("INSERT INTO lines_fts(lines_fts) "
                         "VALUES('integrity-check')").fetchone()
            fts_ok = True
        except sqlite3.OperationalError:
            # older SQLite: count+query equality above is the fallback
            fts_ok = fts_n == lines_n
        check(fts_ok, "FTS5 integrity-check (or count fallback) passes")
        # recognition revision identifiers
        rev_mismatch = 0
        for p in sidecar_root.rglob("*.htr.json"):
            if "revisions" in p.parts:
                continue
            scx = json.loads(p.read_text())
            rev = scx["recognition"]["revision"]
            for ln in scx["lines"]:
                if f"-r{rev}-" not in ln["line_id"]:
                    rev_mismatch += 1
            prow = conn.execute(
                "SELECT provenance FROM pages WHERE page_id=?",
                (scx["source"]["page_id"],)).fetchone()
            if prow and scx["recognition"].get("status") == "ok":
                db_rev = json.loads(prow["provenance"] or "{}").get("revision")
                if db_rev != rev:
                    rev_mismatch += 1
        check(rev_mismatch == 0,
              f"sidecar line ids + DB provenance carry the declared revision "
              f"(mismatches={rev_mismatch})")
        conn.close()

        # 3) CLEAN rebuild #2 — determinism
        new2 = td / "new2.db"
        stats2 = build_test_db(new2, sidecar_root, reviews_path=reviews)
        d1, d2 = dump_db(new1), dump_db(new2)
        check(d1 == d2,
              f"deterministic reconstruction ({d1[:12]} == {d2[:12]})")
        check(stats1 == stats2, "identical build stats across clean builds")
        check(len(load_records({"reviews_path": str(reviews)})) == 2,
              "durable store unchanged by rebuilds")

    if FAIL:
        print(f"FAIL ({len(FAIL)}):")
        for f in FAIL:
            print("  -", f)
        return 1
    print("test_clean_rebuild: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
