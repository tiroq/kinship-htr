#!/usr/bin/env python3
"""P0 — human review integrity tests.

Drives the PRODUCTION write path (durable JSONL store + DB view) and the
real rebuild path, then mutates recognition output the way re-recognition
does, asserting:

  * a review is NEVER deleted (store and DB row counts stay constant);
  * HUMAN-VERIFIED never transfers to a different spatial region, line
    number, recognition revision or image;
  * reordered / inserted / removed / split lines each resolve to an
    explicit `unresolved` reason — never a silent re-bind;
  * reviews survive complete database loss (rebuilt from the store alone).

Run:  htrbench-python tests/test_review_integrity.py   (pytest-compatible)
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from htr_sidecar import (atomic_write_json, build_sidecar,  # noqa: E402
                         read_sidecar, write_sidecar)
from tests._fixtures import add_review, build_test_db  # noqa: E402

FAIL: list[str] = []

SHA = "ab" * 32
REC = {"engine": "crnn-ctc (PyLaia-inspired CRNN-CTC)",
       "model": "achimrabus/crnn-ctc-russian 'Russian (6.92% CER)'",
       "model_sha256": "74" * 32, "segmentation": "kraken-blla",
       "segmentation_sha256": "77" * 32, "patch": "bench-patch.diff",
       "device": "cpu", "status": "ok", "origin": "new",
       "recognized_at": "2026-11-25T12:00:00+0000", "duration_s": 10.0,
       "revision": 1}

# line_number, text, xywh bbox
BASE_LINES = [
    (1, "строка один", [10, 10, 100, 20]),
    (2, "строка два", [10, 40, 100, 20]),
    (3, "строка три", [10, 70, 100, 20]),
]


def check(cond, msg):
    if cond:
        print(f"PASS {msg}")
    else:
        FAIL.append(msg)
        print(f"FAIL {msg}")


def make_page(lines_spec, *, revision: int = 1, sha: str = SHA,
              page_id: str = "t100-p001") -> dict:
    source = {
        "source_ref": f"test:{page_id}", "page_id": page_id,
        "case_ref": f"test:{page_id}", "case_short": "t100",
        "page_number": 1, "original_filename": f"{page_id}.jpg",
        "logical_path": f"test/{page_id}.jpg",
        "image_path": f"/nonexistent/{page_id}.jpg",
        "sha256": sha, "size_bytes": 999, "collection": "alpha",
        "archive": "TEST", "fond": "1", "opis": "1", "delo": "1",
        "scan_number": 1, "source_id": "test", "root": "/nonexistent",
    }
    return build_sidecar(
        source=source, image={"width": 500, "height": 800},
        recognition=dict(REC, revision=revision),
        lines=[{"line_number": n, "text": t, "bbox": xywh,
                "bbox_convention": "xywh", "confidence": 0.9,
                "source": "blla-ocr"} for n, t, xywh in lines_spec])


class Env:
    def __init__(self, td: Path, name: str):
        self.dir = td / name
        self.sidecar_root = self.dir / "sidecars"
        self.sidecar_root.mkdir(parents=True)
        self.db = self.dir / "test.db"
        self.reviews = self.dir / "reviews.jsonl"

    @property
    def sidecar_path(self) -> Path:
        return self.sidecar_root / "t100-p001.htr.json"

    def write_page(self, sidecar: dict, *, direct: bool = True) -> None:
        """direct=True: atomic replace WITHOUT revision bump (same-revision
        artifact mutation); direct=False: production write (bumps revision
        and re-stamps line ids like reprocessing does)."""
        if direct:
            atomic_write_json(self.sidecar_path, sidecar)
        else:
            write_sidecar(self.sidecar_path, sidecar)

    def build(self):
        return build_test_db(self.db, self.sidecar_root,
                             reviews_path=self.reviews)

    def row(self, record_id: str):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM reviews WHERE record_id=?",
                         (record_id,)).fetchone()
        conn.close()
        return r

    def rows(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        out = [dict(r) for r in conn.execute("SELECT * FROM reviews")]
        conn.close()
        return out

    def store_count(self) -> int:
        from htr_reviews import load_records
        return len(load_records({"reviews_path": str(self.reviews)}))


def active_line_text(env: Env, row) -> str | None:
    if not row or row["match_state"] != "active" or row["line_id"] is None:
        return None
    import sqlite3
    conn = sqlite3.connect(env.db)
    r = conn.execute("SELECT text_raw FROM lines WHERE line_id=?",
                     (row["line_id"],)).fetchone()
    conn.close()
    return r[0] if r else None


def setup(td: Path, name: str) -> tuple[Env, dict]:
    env = Env(td, name)
    env.write_page(make_page(BASE_LINES))
    stats = env.build()
    check(stats["pages_ok"] == 1 and stats["lines"] == 3,
          f"{name}: base page built ({stats['pages_ok']}p/{stats['lines']}l)")
    record = add_review(env.db, make_page(BASE_LINES), 2,
                        decision="confirmed",
                        verified_text="строка два — проверено человеком",
                        reviews_path=env.reviews)
    row = env.row(record["record_id"])
    check(row is not None and row["match_state"] == "active"
          and row["line_id"] is not None,
          f"{name}: review active on its own line")
    return env, record


def main() -> int:
    with tempfile.TemporaryDirectory() as tds:
        td = Path(tds)

        # --- 1. reordered lines (same revision) --------------------------
        env, rec = setup(td, "reorder")
        env.write_page(make_page([
            (1, "строка два", [10, 40, 100, 20]),   # line_number 1 -> old L2 bbox
            (2, "строка один", [10, 10, 100, 20]),  # line_number 2 -> old L1 bbox
            (3, "строка три", [10, 70, 100, 20])]))
        env.build()
        row = env.row(rec["record_id"])
        check(row is not None and row["match_state"] == "unresolved" and
              row["match_reason"] == "geometry_changed" and
              row["line_id"] is None,
              f"reorder -> unresolved/geometry_changed (got "
              f"{row and row['match_reason']})")
        check(active_line_text(env, row) is None,
              "reorder: review NOT transferred to the new line_number region")
        check(env.store_count() == 1 and len(env.rows()) == 1,
              "reorder: review never deleted (store=1, db=1)")
        check(row["verified_text"] == "строка два — проверено человеком",
              "reorder: verified text intact")

        # --- 2. inserted line with stable numbers ------------------------
        env, rec = setup(td, "insert-stable")
        env.write_page(make_page(BASE_LINES + [
            (4, "новая строка", [10, 100, 100, 20])]))
        env.build()
        row = env.row(rec["record_id"])
        check(row is not None and row["match_state"] == "active",
              "insert (stable line numbers): review still active")

        # --- 3. inserted line renumbering the reviewed line --------------
        env, rec = setup(td, "insert-renumber")
        env.write_page(make_page([
            (1, "строка один", [10, 10, 100, 20]),
            (2, "вставленная", [10, 25, 100, 14]),   # pushes old L2 to number 3
            (3, "строка два", [10, 40, 100, 20]),
            (4, "строка три", [10, 70, 100, 20])]))
        env.build()
        row = env.row(rec["record_id"])
        check(row is not None and row["match_state"] == "unresolved" and
              row["match_reason"] == "geometry_changed",
              f"insert+renumber -> unresolved/geometry_changed (got "
              f"{row and row['match_reason']})")
        check(active_line_text(env, row) is None,
              "insert+renumber: no auto-transfer to any region")
        check(env.store_count() == 1 and len(env.rows()) == 1,
              "insert+renumber: review preserved")

        # --- 4. removed reviewed line ------------------------------------
        env, rec = setup(td, "remove")
        env.write_page(make_page([BASE_LINES[0], BASE_LINES[2]]))
        env.build()
        row = env.row(rec["record_id"])
        check(row is not None and row["match_state"] == "unresolved" and
              row["match_reason"] == "line_missing" and row["line_id"] is None,
              f"remove -> unresolved/line_missing (got "
              f"{row and row['match_reason']})")
        check(env.store_count() == 1 and len(env.rows()) == 1,
              "remove: review preserved as historical record")

        # --- 5. split reviewed line --------------------------------------
        env, rec = setup(td, "split")
        env.write_page(make_page([
            (1, "строка один", [10, 10, 100, 20]),
            (2, "строка два (верх)", [10, 40, 100, 10]),   # top half
            (4, "строка два (низ)", [10, 50, 100, 10]),    # bottom half
            (3, "строка три", [10, 70, 100, 20])]))
        env.build()
        row = env.row(rec["record_id"])
        check(row is not None and row["match_state"] == "unresolved" and
              row["match_reason"] == "geometry_changed",
              f"split -> unresolved/geometry_changed (got "
              f"{row and row['match_reason']})")
        import sqlite3
        conn = sqlite3.connect(env.db)
        bound = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE match_state='active' AND "
            "line_id IN (SELECT line_id FROM lines WHERE page_id='t100-p001')"
        ).fetchone()[0]
        conn.close()
        check(bound == 0, "split: neither half inherited HUMAN-VERIFIED")
        check(env.store_count() == 1 and len(env.rows()) == 1,
              "split: review preserved")

        # --- 6. re-recognition (revision bump) ---------------------------
        env, rec = setup(td, "revision-bump")
        # production write path: write_sidecar bumps rev1 -> rev2 and
        # re-stamps line ids
        env.write_page(make_page(BASE_LINES, revision=2), direct=False)
        check(read_sidecar(env.sidecar_path)["recognition"]["revision"] == 2,
              "revision bumped to 2 by production write")
        check(all(ln["line_id"].startswith("t100-p000-r2-") or
                  ln["line_id"].startswith("t100-p001-r2-")
                  for ln in read_sidecar(env.sidecar_path)["lines"]),
              "line ids re-stamped with the new revision")
        env.build()
        row = env.row(rec["record_id"])
        check(row is not None and row["match_state"] == "unresolved" and
              row["match_reason"] == "revision_changed" and
              row["line_id"] is None,
              f"revision bump -> unresolved/revision_changed even with "
              f"identical geometry (got {row and row['match_reason']})")
        check(env.store_count() == 1 and len(env.rows()) == 1,
              "revision bump: review preserved, never re-bound")

        # --- 7. image content change -------------------------------------
        env, rec = setup(td, "image-change")
        env.write_page(make_page(BASE_LINES, sha="cd" * 32))
        env.build()
        row = env.row(rec["record_id"])
        check(row is not None and row["match_state"] == "unresolved" and
              row["match_reason"] == "image_changed",
              f"other image -> unresolved/image_changed (got "
              f"{row and row['match_reason']})")

        # --- 8. complete database loss -----------------------------------
        env, rec = setup(td, "db-loss")
        for suffix in ("", "-wal", "-shm"):   # the whole index is gone
            (Path(str(env.db) + suffix)).unlink(missing_ok=True)
        stats = env.build()                # rebuild from sidecars + store
        row = env.row(rec["record_id"])
        check(row is not None and row["match_state"] == "active" and
              row["line_id"] is not None,
              "DB loss: review recovered from durable store, active again")
        check(stats["reviews_active"] == 1 and stats["reviews_unresolved"] == 0,
              f"DB loss: counts restored ({stats['reviews_active']}/"
              f"{stats['reviews_unresolved']})")

        # --- 9. two records on one line: one active, one superseded ------
        env, rec = setup(td, "supersede")
        from htr_reviews import load_records
        first_id = rec["record_id"]
        second = add_review(env.db, make_page(BASE_LINES), 2,
                            decision="uncertain", verified_text=None,
                            notes="second reviewer",
                            reviews_path=env.reviews)
        rows = env.rows()
        active = [r for r in rows if r["match_state"] == "active"]
        superseded = [r for r in rows if r["match_reason"] == "superseded"]
        check(len(active) == 1 and active[0]["record_id"] == second["record_id"],
              "supersede: latest record is the active one")
        check(len(superseded) == 1 and
              superseded[0]["record_id"] == first_id,
              "supersede: displaced review kept as unresolved/superseded")
        check(env.store_count() == 2 and len(env.rows()) == 2,
              "supersede: both records preserved everywhere")
        # rebuild keeps both, exactly one active
        env.build()
        rows = env.rows()
        check(sum(r["match_state"] == "active" for r in rows) == 1 and
              len(rows) == 2 and env.store_count() == 2,
              "supersede: stable across rebuild (2 records, 1 active)")

        # --- 11. sidecar removed: page pruned, review preserved ----------
        env, rec = setup(td, "sidecar-removed")
        env.sidecar_path.unlink()          # portable artifact is gone
        stats = env.build()
        import sqlite3 as _sq
        c = _sq.connect(env.db)
        pages_n = c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        c.close()
        check(pages_n == 0 and stats.get("pages_pruned") == 1,
              f"stale page pruned on rebuild ({stats.get('pages_pruned')})")
        row = env.row(rec["record_id"])
        check(row is not None and row["match_state"] == "unresolved" and
              row["match_reason"] == "page_missing" and row["line_id"] is None,
              f"review of pruned page preserved as unresolved "
              f"({row and row['match_reason']})")
        check(env.store_count() == 1 and len(env.rows()) == 1,
              "prune: review never deleted")

        # --- 12. search never labels an unresolved review ----------------
        env, rec = setup(td, "search-label")
        env.write_page(make_page([
            (1, "строка один", [10, 10, 100, 20]),
            (3, "строка три", [10, 70, 100, 20])]))      # reviewed line gone
        env.build()
        from htr_search.search import Searcher
        s = Searcher(db_path=env.db)
        hits = s.search_lines("строка")
        s.close()
        labeled = [h for h in hits if h.evidence_class == "human-verified"]
        check(not labeled,
              "search: no hit carries HUMAN-VERIFIED after line disappeared")
        check(all(h.lead_status == "SECONDARY-LEAD" for h in hits),
              "search: all machine hits stay SECONDARY-LEAD")

    if FAIL:
        print(f"FAIL ({len(FAIL)}):")
        for f in FAIL:
            print("  -", f)
        return 1
    print("test_review_integrity: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
