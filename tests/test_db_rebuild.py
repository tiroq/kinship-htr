#!/usr/bin/env python3
"""Self-contained tests: DB rebuild from portable sidecars + transactional
upserts (htr_search.db).

Covers: full rebuild without HTR, xywh/xyxy conversion at the DB boundary,
FTS5 searchability, idempotent re-upsert, transactional line replacement,
review preservation across rebuilds (stable page_id+line_number remap),
failed/duplicate page visibility, and archival filter columns.

Run:  htrbench-python tests/test_db_rebuild.py   (pytest-compatible)
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._fixtures import build_test_db, make_sidecars  # noqa: E402

FAIL: list[str] = []


def check(cond, msg):
    if cond:
        print(f"PASS {msg}")
    else:
        FAIL.append(msg)
        print(f"FAIL {msg}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tds:
        td = Path(tds)
        sidecar_root = td / "sidecars"
        sidecar_root.mkdir()
        db_path = td / "test.db"
        make_sidecars(sidecar_root)

        # --- 1. full rebuild from sidecars only -------------------------
        stats = build_test_db(db_path, sidecar_root)
        check(stats["pages_ok"] == 2,
              f"2 ok pages indexed: {stats['pages_ok']}")
        check(stats["pages_failed"] == 1, "failed page visible")
        check(stats["pages_duplicate"] == 1, "duplicate page visible")
        check(stats["lines"] == 5, f"5 lines total: {stats['lines']}")
        check(stats["invalid_sidecars"] == [],
              f"no invalid sidecars: {stats['invalid_sidecars']}")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # --- 2. bbox stored as xywh for legacy consumers ----------------
        row = conn.execute(
            "SELECT bbox, y_center, text_raw FROM lines WHERE page_id='g999-p000' "
            "AND line_number=1").fetchone()
        bbox = json.loads(row["bbox"])
        check(bbox == [10, 20, 300, 40],
              f"DB bbox is xywh converted from sidecar xyxy: {bbox}")
        check(abs(row["y_center"] - (20 + 40 / 2)) < 1e-9,
              f"y_center from xyxy: {row['y_center']}")
        check(row["text_raw"].startswith("Той же"), "raw diplomatic text kept")

        # --- 3. FTS5 + trigram searchable -------------------------------
        hits = conn.execute(
            "SELECT rowid FROM lines_fts WHERE lines_fts MATCH 'волченок' "
            "LIMIT 5").fetchall()
        check(len(hits) >= 1, "FTS5 finds normalized historical spelling")
        hits2 = conn.execute(
            "SELECT rowid FROM lines_trgm WHERE lines_trgm MATCH 'олченок' "
            "LIMIT 5").fetchall()
        check(len(hits2) >= 1, "trigram index recalls partial strings")

        # --- 4. archival filter columns ---------------------------------
        r = conn.execute("SELECT collection, fond, opis, delo FROM pages "
                         "WHERE page_id='g999-p000'").fetchone()
        check((r["collection"], r["fond"], r["opis"], r["delo"]) ==
              ("gavo", "И331", "3", "999"),
              f"archival columns: {tuple(r)}")

        # --- 5. failed / duplicate rows carry diagnostics ----------------
        f = conn.execute("SELECT status, error FROM pages "
                         "WHERE page_id='g998-p000'").fetchone()
        check(f["status"] == "failed" and "segmentation" in (f["error"] or ""),
              f"failed page keeps error: {f['error']!r}")
        n = conn.execute("SELECT COUNT(*) FROM lines "
                         "WHERE page_id='g998-p000'").fetchone()[0]
        check(n == 0, "failed page has no lines")
        d = conn.execute("SELECT status, provenance, error FROM pages "
                         "WHERE page_id='g997-p000'").fetchone()
        check(d["status"] == "duplicate" and
              "g999-p000" in (d["provenance"] or "") and
              "g999-p000" in (d["error"] or ""),
              f"duplicate points at canonical page: {d['provenance']}")

        # --- 6. review survives rebuild via (page_id, line_number) -------
        line_id = conn.execute(
            "SELECT line_id FROM lines WHERE page_id='g999-p000' "
            "AND line_number=1").fetchone()["line_id"]
        conn.execute(
            "INSERT INTO reviews(line_id, page_id, line_number, decision, "
            "verified_text, notes, source_sha256, case_id, page_number, "
            "bbox, model_text, reviewer, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (line_id, "g999-p000", 1, "confirmed",
             "Той же вотчины деревни Волченокъ крестьянин", "human read",
             "aa" * 32, "gavo-I331-3-999", 0, "[10,20,300,40]",
             "machine", "tester", "2026-11-25T12:00:00+0000"))
        conn.commit()
        conn.close()

        stats2 = build_test_db(db_path, sidecar_root)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rev = conn.execute("SELECT * FROM reviews WHERE page_id='g999-p000' "
                           "AND line_number=1").fetchone()
        new_line_id = conn.execute(
            "SELECT line_id FROM lines WHERE page_id='g999-p000' "
            "AND line_number=1").fetchone()["line_id"]
        check(rev is not None and rev["decision"] == "confirmed" and
              rev["verified_text"].startswith("Той же"),
              "human review decision preserved across rebuild")
        check(rev is not None and rev["line_id"] == new_line_id,
              "review remapped onto the new line_id")
        check(stats2["reviews_remapped"] >= 1,
              f"remap counted: {stats2['reviews_remapped']}")
        check(conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1,
              "exactly one review row (no duplication)")

        # --- 7. transactional idempotent upsert -------------------------
        from htr_search.db import upsert_page
        sc = json.loads((sidecar_root / "g999-p000.htr.json").read_text())
        for _ in range(3):
            upsert_page(db_path, sc)
        n = conn.execute("SELECT COUNT(*) FROM lines "
                         "WHERE page_id='g999-p000'").fetchone()[0]
        check(n == 3, f"re-upsert is idempotent (3 lines, got {n})")
        rev2 = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        check(rev2 == 1, "review survives repeated upserts")

        # changed sidecar replaces lines and FTS content
        sc["lines"][0]["text_raw"] = "исправленная строка"
        sc["lines"][0]["text_norm"] = "исправленная строка"
        upsert_page(db_path, sc)
        old = conn.execute(
            "SELECT rowid FROM lines_fts WHERE lines_fts MATCH 'крестьянин' "
            "AND rowid IN (SELECT line_id FROM lines WHERE page_id='g999-p000')"
        ).fetchall()
        new = conn.execute(
            "SELECT rowid FROM lines_fts WHERE lines_fts MATCH 'исправленная'"
        ).fetchall()
        check(len(old) == 0 and len(new) >= 1,
              "FTS updated transactionally on line replacement")
        check(conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1,
              "review row not silently invalidated by replacement")

        # failed upsert must not corrupt: simulate bad sidecar
        bad = json.loads(json.dumps(sc))
        bad["source"]["page_id"] = ""            # violates NOT NULL-ish flow
        try:
            upsert_page(db_path, bad)
            # empty page_id would insert; ensure lines of g999 untouched
        except Exception:
            pass
        n = conn.execute("SELECT COUNT(*) FROM lines "
                         "WHERE page_id='g999-p000'").fetchone()[0]
        check(n == 3, "existing page untouched by unrelated upsert")

        # --- 8. search availability shape (reader opens while WAL) -------
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        check(str(mode).lower() == "wal", f"journal mode WAL: {mode}")
        conn.close()

    if FAIL:
        print(f"FAIL ({len(FAIL)}):")
        for f in FAIL:
            print("  -", f)
        return 1
    print("test_db_rebuild: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
