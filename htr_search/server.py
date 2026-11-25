#!/usr/bin/env python3
"""Stage E — local web review interface (stdlib http.server, no frameworks).

Endpoints:
  GET  /                     review app (single file, vanilla JS)
  GET  /api/search           q, profile, case, limit, review_filter
  GET  /api/line/<id>        line + page + neighbors + review + provisional ref
  GET  /api/image/<page_id>  original scan (read-only stream)
  GET  /api/crop/<line_id>?pad=  cropped JPEG around the bbox
  GET  /api/pages            page list w/ coverage + review status
  GET  /api/stats            index stats
  GET  /api/profiles         search profiles
  GET  /api/export           json|csv, decision filter
  POST /api/review           {line_id, decision, verified_text?, notes?}

Review POSTs write an evidence record (source SHA-256, case, page,
coordinates, exact human transcription, decision, timestamp) into
`reviews`.  AI-draft provisional references are read-only and never
promoted to human ground truth.
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from htr_search.db import DB_PATH  # noqa: E402
from htr_search.profiles import PROFILES, profile_variants  # noqa: E402
from htr_search.search import Searcher, hit_to_dict  # noqa: E402

STATIC = Path(__file__).parent / "app.html"
ALLOWED_IMAGE = re.compile(r"^[0-9]{3}-p[0-9]{3,}$")


class Handler(BaseHTTPRequestHandler):
    server_version = "htr-review/1.0"

    def log_message(self, fmt, *args):  # quieter console
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ------------- helpers -------------------------------------------

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _searcher(self) -> Searcher:
        return Searcher()

    def _q(self) -> dict:
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    # ------------- GET ------------------------------------------------

    def do_GET(self):
        route, _, _ = self.path.partition("?")
        try:
            if route in ("/", "/index.html"):
                body = STATIC.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if route == "/api/search":
                return self.api_search()
            if route == "/api/pages":
                return self.api_pages()
            if route == "/api/stats":
                return self.api_stats()
            if route == "/api/profiles":
                return self.api_profiles()
            if route == "/api/export":
                return self.api_export()
            m = re.match(r"^/api/line/(\d+)$", route)
            if m:
                return self.api_line(int(m.group(1)))
            m = re.match(r"^/api/image/([0-9]{3}-p[0-9]{3,})$", route)
            if m:
                return self.api_image(m.group(1))
            m = re.match(r"^/api/crop/(\d+)$", route)
            if m:
                return self.api_crop(int(m.group(1)), self._q())
            self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # surface as JSON, keep serving
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def api_search(self):
        q = self._q()
        query = q.get("q", "").strip()
        if not query:
            return self._json({"hits": [], "pages": [], "query": ""})
        s = self._searcher()
        variants = None
        if q.get("profile") in PROFILES:
            variants = profile_variants(q["profile"])["all"]
        case = q.get("case") or None
        hits = s.search_lines(query, variants=variants, case=case,
                              limit=int(q.get("limit", 100)))
        rf = q.get("review_filter")
        if rf == "reviewed":
            hits = [h for h in hits if h.review]
        elif rf == "unreviewed":
            hits = [h for h in hits if not h.review]
        elif rf == "confirmed":
            hits = [h for h in hits if h.review and h.review["decision"] == "confirmed"]
        out = [hit_to_dict(h) for h in hits]
        pages: dict[str, dict] = {}
        for d in out:
            e = pages.setdefault(d["page_id"], {"page_id": d["page_id"],
                                                "best": 0.0, "hits": []})
            e["hits"].append(d)
            e["best"] = max(e["best"], d["score"])
        s.close()
        return self._json({"query": query,
                            "profile": q.get("profile"),
                            "hits": out,
                            "pages": sorted(pages.values(),
                                            key=lambda e: -e["best"])})

    def api_line(self, line_id: int):
        s = self._searcher()
        row = s.conn.execute(
            "SELECT l.*, p.case_short, p.case_id, p.page_number, p.height, "
            "p.width, p.image_path, p.image_sha256, p.status "
            "FROM lines l JOIN pages p USING(page_id) WHERE l.line_id=?",
            (line_id,)).fetchone()
        if not row:
            s.close()
            return self._json({"error": "no such line"}, 404)
        prov = s.conn.execute(
            "SELECT text, provenance, verified_flag FROM provisional_refs "
            "WHERE page_id=? AND line_number=?",
            (row["page_id"], row["line_number"])).fetchone()
        review = s.conn.execute(
            "SELECT decision, verified_text, notes, updated_at, reviewer "
            "FROM reviews WHERE line_id=?", (line_id,)).fetchone()
        s.close()
        return self._json({
            "line": dict(row),
            "neighbors": [dict(r) for r in self._neighbors(line_id)],
            "provisional_reference": dict(prov) if prov else None,
            "review": dict(review) if review else None,
            "cross_page": {},
        })

    def _neighbors(self, line_id: int, k: int = 4):
        conn = Searcher().conn
        row = conn.execute("SELECT page_id, bbox FROM lines WHERE line_id=?",
                           (line_id,)).fetchone()
        if not row:
            return []
        rows = conn.execute(
            "SELECT line_id, line_number, source, text_raw, bbox, confidence "
            "FROM lines WHERE page_id=? ORDER BY y_center, reading_order",
            (row["page_id"],)).fetchall()
        idx = next((i for i, r in enumerate(rows)
                    if r["line_id"] == line_id), None)
        if idx is None:
            return []
        return [rows[j] for j in range(max(0, idx - k),
                                       min(len(rows), idx + k + 1)) if j != idx]

    def api_image(self, page_id: str):
        conn = Searcher().conn
        row = conn.execute("SELECT image_path FROM pages WHERE page_id=?",
                           (page_id,)).fetchone()
        conn.close()
        if not row:
            return self._json({"error": "no such page"}, 404)
        path = Path(row["image_path"])
        if not path.exists():
            return self._json({"error": "image missing"}, 404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def api_crop(self, line_id: int, q: dict):
        conn = Searcher().conn
        row = conn.execute(
            "SELECT l.bbox, p.image_path, p.width, p.height "
            "FROM lines l JOIN pages p USING(page_id) WHERE l.line_id=?",
            (line_id,)).fetchone()
        conn.close()
        if not row:
            return self._json({"error": "no such line"}, 404)
        x, y, w, h = json.loads(row["bbox"])
        pad = min(int(q.get("pad", 120)), 400)
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1 = min(row["width"] or 10**9, x + w + pad)
        y1 = min(row["height"] or 10**9, y + h + pad)
        img = Image.open(row["image_path"]).convert("RGB").crop((x0, y0, x1, y1))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
        body = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def api_pages(self):
        conn = Searcher().conn
        q = self._q()
        where, args = "1=1", []
        if q.get("case"):
            where += " AND case_short=?"
            args.append(q["case"])
        rows = conn.execute(
            f"SELECT p.*, (SELECT COUNT(*) FROM reviews r WHERE r.page_id="
            f"p.page_id) AS reviewed FROM pages p WHERE {where} "
            f"ORDER BY p.case_short, p.page_number", args).fetchall()
        conn.close()
        return self._json({"pages": [dict(r) for r in rows]})

    def api_stats(self):
        conn = Searcher().conn
        stats = {r[0]: r[1] for r in conn.execute(
            "SELECT 'pages', COUNT(*) FROM pages UNION ALL "
            "SELECT 'pages_ok', COUNT(*) FROM pages WHERE status='ok' UNION ALL "
            "SELECT 'pages_failed', COUNT(*) FROM pages WHERE status='failed' UNION ALL "
            "SELECT 'lines', COUNT(*) FROM lines UNION ALL "
            "SELECT 'lines_stage_b', COUNT(*) FROM lines WHERE source='stage-b-gap' UNION ALL "
            "SELECT 'reviews', COUNT(*) FROM reviews UNION ALL "
            "SELECT 'confirmed', COUNT(*) FROM reviews WHERE decision='confirmed' UNION ALL "
            "SELECT 'rejected', COUNT(*) FROM reviews WHERE decision='rejected' UNION ALL "
            "SELECT 'uncertain', COUNT(*) FROM reviews WHERE decision='uncertain'")}
        conn.close()
        return self._json(stats)

    def api_profiles(self):
        return self._json({name: profile_variants(name)
                           for name in PROFILES})

    def api_export(self):
        q = self._q()
        fmt = q.get("format", "json")
        conn = Searcher().conn
        where, args = "1=1", []
        if q.get("decision"):
            where += " AND r.decision=?"
            args.append(q["decision"])
        if q.get("case"):
            where += " AND p.case_short=?"
            args.append(q["case"])
        rows = conn.execute(
            "SELECT r.line_id, r.page_id, p.case_id, p.page_number, "
            "r.decision, r.verified_text, r.notes, r.source_sha256, r.bbox, "
            "r.model_text, r.reviewer, r.updated_at "
            "FROM reviews r JOIN pages p USING(page_id) "
            f"WHERE {where} ORDER BY p.case_short, p.page_number, r.line_id",
            args).fetchall()
        conn.close()
        ts = time.strftime("%Y%m%d-%H%M%S")
        if fmt == "csv":
            import csv
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["line_id", "page_id", "case_id", "page_number",
                        "decision", "verified_text", "notes", "source_sha256",
                        "bbox", "model_text", "reviewer", "updated_at"])
            for r in rows:
                w.writerow([r[k] for k in
                             ("line_id", "page_id", "case_id", "page_number",
                              "decision", "verified_text", "notes",
                              "source_sha256", "bbox", "model_text",
                              "reviewer", "updated_at")])
            body = buf.getvalue().encode()
            ctype = "text/csv; charset=utf-8"
            fname = f"reviews-{ts}.csv"
        else:
            body = json.dumps([dict(r) for r in rows],
                              ensure_ascii=False, indent=1).encode()
            ctype = "application/json; charset=utf-8"
            fname = f"reviews-{ts}.json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------- POST ----------------------------------------------

    def do_POST(self):
        if not self.path.startswith("/api/review"):
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            self._review(data)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 400)

    def _review(self, data: dict):
        line_id = int(data.get("line_id", 0))
        decision = data.get("decision")
        if decision not in ("confirmed", "rejected", "uncertain"):
            return self._json({"error": "decision must be confirmed|"
                              "rejected|uncertain"}, 400)
        verified_text = (data.get("verified_text") or "").strip()
        if decision == "confirmed" and not verified_text:
            return self._json({"error": "confirmed requires a verified "
                              "transcription"}, 400)
        s = self._searcher()
        row = s.conn.execute(
            "SELECT l.page_id, l.line_number, l.bbox, l.text_raw, "
            "p.case_id, p.page_number, p.image_sha256 "
            "FROM lines l JOIN pages p USING(page_id) WHERE l.line_id=?",
            (line_id,)).fetchone()
        if not row:
            s.close()
            return self._json({"error": "no such line"}, 404)
        notes = (data.get("notes") or "").strip() or None
        # Human-supplied text only; provisional/AI drafts are never read here.
        s.conn.execute(
            "INSERT INTO reviews(line_id, page_id, line_number, decision, verified_text, "
            "notes, source_sha256, case_id, page_number, bbox, model_text, "
            "reviewer, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(line_id) DO UPDATE SET decision=excluded.decision, "
            "verified_text=excluded.verified_text, notes=excluded.notes, "
            "updated_at=excluded.updated_at",
            (line_id, row["page_id"], row["line_number"], decision, verified_text or None, notes,
             row["image_sha256"], row["case_id"], row["page_number"],
             row["bbox"], row["text_raw"],
             data.get("reviewer", "local-human"),
             time.strftime("%Y-%m-%dT%H:%M:%S%z")))
        s.conn.commit()
        s.close()
        self._json({"ok": True, "line_id": line_id, "decision": decision})


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    if not DB_PATH.exists():
        print(f"index missing: {DB_PATH} — build it first", file=sys.stderr)
        return 2
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"review UI on http://127.0.0.1:{port}/  (db={DB_PATH})")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
