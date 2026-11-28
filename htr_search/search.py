"""Search core: exact FTS, trigram recall, RapidFuzz similarity ranking.

Ranking is configurable (work/index/ranking.json) with independent components:
  text    — best partial_ratio over query variants (0-100)
  context — best similarity among spatial neighbours (0-100, damped)
  layout  — position/source points (headers, stage-B gap lines, margins)
  conf    — OCR-confidence *hint*, capped small; NOT treated as calibrated
            accuracy and never allowed to dominate.

A positive match is a lead; a negative match is NOT evidence of absence.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz, process
from .normalize import normalize, trigrams

BENCH = Path("/Users/mysterx/dev/kinship-htr-bench")
RANKING_PATH = BENCH / "work" / "index" / "ranking.json"

DEFAULT_WEIGHTS = {
    "w_text": 1.0,
    "w_context": 0.25,
    "w_layout": 0.20,
    "conf_hint_max": 0.03,     # cap: confidence contributes <= 3 points of 100
    "min_text_score": 55.0,    # below this a line is not a candidate
    "trigram_prefilter_cap": 30000,
}


def load_weights() -> dict:
    w = dict(DEFAULT_WEIGHTS)
    if RANKING_PATH.exists():
        w.update(json.loads(RANKING_PATH.read_text()))
    return w


def save_weights(w: dict) -> None:
    RANKING_PATH.parent.mkdir(parents=True, exist_ok=True)
    RANKING_PATH.write_text(json.dumps(w, ensure_ascii=False, indent=1))


@dataclass
class Hit:
    line_id: int
    page_id: str
    line_number: int
    source: str
    text_raw: str
    bbox: list
    confidence: float | None
    text_score: float
    context_score: float
    layout_score: float
    total: float
    matched_variant: str
    neighbors: list = field(default_factory=list)
    review: dict | None = None
    provisional: str | None = None
    # evidence classification (§ search results must distinguish these):
    match_kind: str = ""            # exact-raw | exact-norm | fuzzy | human-verified
    evidence_class: str = ""        # machine-raw | machine-normalized |
    #                                machine-fuzzy | human-verified
    lead_status: str = ""           # SECONDARY-LEAD | HUMAN-VERIFIED


def classify_hit(h: Hit, query: str, qn: str) -> tuple[str, str, str]:
    """Label a hit's evidence type.  Every machine reading is a
    SECONDARY-LEAD; only a human-confirmed review decision is not."""
    rev = h.review or {}
    if rev.get("decision") == "confirmed":
        return "human-verified", "human-verified", "HUMAN-VERIFIED"
    raw = " ".join(h.text_raw.split()).casefold()
    q = " ".join(query.split()).casefold()
    if q and q in raw:
        return "exact-raw", "machine-raw", "SECONDARY-LEAD"
    if qn and qn in normalize(h.text_raw):
        return "exact-norm", "machine-normalized", "SECONDARY-LEAD"
    return "fuzzy", "machine-fuzzy", "SECONDARY-LEAD"


class Searcher:
    def __init__(self, db_path: Path | str | None = None, weights: dict | None = None):
        from .db import DB_PATH, connect
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.conn: sqlite3.Connection = connect(self.db_path, readonly=False)
        self.conn.row_factory = sqlite3.Row
        self.w = weights or load_weights()
        self._lines_cache: list[tuple[int, str, str, float]] | None = None
    def close(self) -> None:
        self.conn.close()

    # ---------- internals -------------------------------------------------

    def _fts_candidates(self, variants: list[str]) -> set[int]:
        """Exact/prefix FTS hits for any variant (any token, prefix match)."""
        out: set[int] = set()
        for v in variants:
            toks = [t for t in v.split() if len(t) >= 2]
            if not toks:
                continue
            match = " OR ".join(f'"{t}"*' for t in toks[:8])
            try:
                rows = self.conn.execute(
                    "SELECT rowid FROM lines_fts WHERE lines_fts MATCH ? LIMIT 5000",
                    (match,)).fetchall()
            except sqlite3.OperationalError:
                continue
            out.update(r["rowid"] for r in rows)
        return out

    def _trigram_candidates(self, variants: list[str]) -> set[int]:
        """Lines sharing at least one query trigram — the fuzzy recall net."""
        grams: set[str] = set()
        for v in variants:
            grams.update(trigrams(v))
        if not grams:
            return set()
        cap = int(self.w["trigram_prefilter_cap"])
        out: set[int] = set()
        grams = sorted(grams)
        for i in range(0, len(grams), 24):
            chunk = grams[i:i + 24]
            match = " OR ".join(f'"{g}"' for g in chunk)
            try:
                rows = self.conn.execute(
                    "SELECT rowid FROM lines_trgm WHERE lines_trgm MATCH ? LIMIT ?",
                    (match, cap)).fetchall()
            except sqlite3.OperationalError:
                continue
            out.update(r["rowid"] for r in rows)
            if len(out) >= cap:
                break
        return out

    def _line_rows(self, ids: set[int],
                   filters: dict | None = None) -> list[sqlite3.Row]:
        if not ids:
            return []
        ids = sorted(ids)
        out = []
        fsql, fparams = "", []
        if filters:
            clauses = []
            for col, key in (("case_short", "case"),
                             ("collection", "collection"),
                             ("fond", "fond"), ("opis", "opis"),
                             ("delo", "delo"), ("page_id", "page")):
                val = filters.get(key)
                if val:
                    # A duplicate page stores no lines of its own; its
                    # recognized text lives on the canonical page.  Accept a
                    # row when ITS column matches OR when a duplicate
                    # alias of it (canonical_page_id = p.page_id) matches —
                    # so filtering a duplicate collection still retrieves
                    # the recognized text.
                    clauses.append(
                        f"(p.{col}=? OR EXISTS(SELECT 1 FROM pages al "
                        f"WHERE al.canonical_page_id=p.page_id "
                        f"AND al.{col}=?))")
                    fparams.extend([str(val), str(val)])
            if clauses:
                fsql = " AND " + " AND ".join(clauses)
        for i in range(0, len(ids), 900):
            q = ",".join("?" * min(900, len(ids) - i))
            out.extend(self.conn.execute(
                f"SELECT l.*, p.case_short, p.page_number, p.height, p.width "
                f"FROM lines l JOIN pages p USING(page_id) "
                f"WHERE l.line_id IN ({q}){fsql}",
                ids[i:i + 900] + fparams).fetchall())
        return out

    def _neighbors(self, page_id: str, bbox: list, y_center: float, k: int = 3):
        x, y, w, h = bbox
        rows = self.conn.execute(
            "SELECT line_id, line_number, source, text_raw, bbox, confidence "
            "FROM lines WHERE page_id=? ORDER BY ABS(y_center - ?) LIMIT ?",
            (page_id, y_center, k + 1)).fetchall()
        return [dict(r) for r in rows if r["line_id"] is not None][1:]

    def _layout_points(self, row) -> float:
        pts = 0.0
        bbox = json.loads(row["bbox"])
        y, h = bbox[1], bbox[3]
        top = row["height"] or 2354
        if row["source"] == "stage-b-gap":
            pts += 25.0
            if y < 0.15 * top:
                pts += 40.0
        elif y < 0.12 * top:
            pts += 15.0   # upper-page line from main pass
        x, w = bbox[0], bbox[2]
        if x < 0.08 * (row["width"] or 2850) or x + w > 0.92 * (row["width"] or 2850):
            pts += 10.0   # marginal content
        conf = row["confidence"]
        if conf is not None:
            pts += float(self.w["conf_hint_max"]) * 100.0 * float(conf)
        return pts

    # ---------- public API -------------------------------------------------

    def search_lines(self, query: str, variants: list[str] | None = None,
                     case: str | None = None, limit: int = 200,
                     with_neighbors: bool = True,
                     collection: str | None = None,
                     fond: str | None = None, opis: str | None = None,
                     delo: str | None = None, page: str | None = None,
                     exact: bool = False) -> list[Hit]:
        """Search individual OCR lines.  `variants` may come from a profile;
        defaults to the normalized query itself (fuzzy recall handles
        1-3 character corruptions without enumerating them).

        Filters: case (short id), collection, fond/opis/delo (archival
        refs, text match), page (full page_id).  exact=True keeps only
        lines whose text contains the query after normalization (phrase
        semantics over the normalized form).
        """
        qn = normalize(query)
        if not qn:
            return []
        vs = [normalize(v) for v in (variants or [query])]
        vs = [v for v in dict.fromkeys(vs) if v] or [qn]

        cand = self._fts_candidates(vs) | self._trigram_candidates(vs)
        filters = {"case": case, "collection": collection, "fond": fond,
                   "opis": opis, "delo": delo, "page": page}
        rows = self._line_rows(cand, filters)

        scored: list[Hit] = []
        vh = {v: None for v in vs}
        for row in rows:
            if case and row["case_short"] != case:
                continue
            text = row["text_norm"]
            if not text:
                continue  # empty/unreadable OCR: no text evidence, never a hit
            if exact and qn not in text:
                continue  # phrase semantics over the normalized form
            best_s, best_v = 0.0, ""
            for v in vs:
                s = fuzz.partial_ratio(v, text)
                # Short-fragment damping (KINSHIP-HTR-FAMILY-01 Phase A):
                # partial_ratio scores a 2-4 char line («нов», «деми») as
                # 100 whenever it is a substring of the variant, letting
                # fragments outrank full-length genuine matches.  Scale by
                # coverage of the variant so they cannot.
                if len(text) < len(v):
                    s *= len(text) / len(v)
                if s > best_s:
                    best_s, best_v = s, v
                if best_s == 100.0:
                    break
            if best_s < self.w["min_text_score"]:
                continue

            bbox = json.loads(row["bbox"])
            ctx = 0.0
            if with_neighbors:
                for nb in self._neighbors(row["page_id"], bbox, row["y_center"]):
                    nb_text = normalize(nb["text_raw"])
                    if not nb_text:
                        continue
                    ctx = max(ctx, max(
                        (fuzz.partial_ratio(v, nb_text) for v in vs), default=0.0))
            layout = self._layout_points(row)
            w_ = self.w
            total = (w_["w_text"] * best_s + w_["w_context"] * ctx * 0.6
                     + w_["w_layout"] * layout)
            scored.append(Hit(
                line_id=row["line_id"], page_id=row["page_id"],
                line_number=row["line_number"], source=row["source"],
                text_raw=row["text_raw"], bbox=bbox,
                confidence=row["confidence"],
                text_score=best_s, context_score=ctx, layout_score=layout,
                total=total, matched_variant=best_v))

        scored.sort(key=lambda h: (-h.total, h.page_id, h.line_number))
        # limit=None keeps the full scored pool: callers that re-rank by
        # their own keys (sections, tiers) must not lose candidates to a
        # premature corpus-wide trim (KINSHIP-HTR-FAMILY-01 Phase A).
        if limit:
            scored = scored[:limit]

        reviews = {r["line_id"]: r for r in self._reviews_for(
            [h.line_id for h in scored])}
        provs = self._provisional_for(scored)
        for h in scored:
            h.review = reviews.get(h.line_id)
            h.provisional = provs.get((h.page_id, h.line_number)) or None
            h.match_kind, h.evidence_class, h.lead_status = classify_hit(
                h, query, qn)
            if with_neighbors:
                h.neighbors = self._neighbors_full(h.page_id, h.bbox)
        return scored

    def _neighbors_full(self, page_id: str, bbox: list, k: int = 4):
        """Spatial neighbours: k lines above and below in page reading order."""
        rows = self.conn.execute(
            "SELECT line_id, line_number, source, text_raw, bbox, confidence "
            "FROM lines WHERE page_id=? ORDER BY y_center, reading_order",
            (page_id,)).fetchall()
        idx = next((i for i, r in enumerate(rows)
                    if json.loads(r["bbox"]) == bbox), None)
        if idx is None:
            return []
        return [dict(rows[j]) for j in
                range(max(0, idx - k), min(len(rows), idx + k + 1)) if j != idx]

    def _reviews_for(self, line_ids: list[int]) -> list[dict]:
        """ACTIVE human review decisions for the given line ids.

        Unresolved/historical review rows (line_id NULL) are never returned
        here — they carry no current binding and cannot label a hit."""
        if not line_ids:
            return []
        ph = ",".join("?" * len(line_ids))
        return [dict(r) for r in self.conn.execute(
            "SELECT line_id, page_id, decision, verified_text, notes "
            f"FROM reviews WHERE match_state='active' AND line_id IN ({ph})",
            line_ids)]

    def _provisional_for(self, hits: list[Hit]) -> dict:
        out = {}
        for h in hits:
            row = self.conn.execute(
                "SELECT text FROM provisional_refs WHERE page_id=? AND line_number=?",
                (h.page_id, h.line_number)).fetchone()
            if row and row["text"]:
                out[(h.page_id, h.line_number)] = row["text"]
        return out

    def search_multiline(self, query: str, case: str | None = None,
                         limit: int = 100, **filters) -> dict:
        """Multi-line query: each line searched separately, results merged
        per page (page score = sum of best line scores per query line).
        Extra keyword filters (collection/fond/opis/delo/page/exact) are
        forwarded to every sub-search."""
        subqueries = [q for q in (line.strip() for line in query.splitlines())
                      if q.strip()]
        per_page: dict[str, dict] = {}
        for sq in subqueries:
            best_by_page: dict[str, Hit] = {}
            for h in self.search_lines(sq, case=case, limit=500,
                                       with_neighbors=False, **filters):
                cur = best_by_page.get(h.page_id)
                if cur is None or h.total > cur.total:
                    best_by_page[h.page_id] = h
            for pid, h in best_by_page.items():
                e = per_page.setdefault(pid, {"hits": [], "score": 0.0})
                e["hits"].append(h)
                e["score"] += h.total
        pages = sorted(per_page.items(), key=lambda kv: -kv[1]["score"])[:limit]
        return {"pages": [{"page_id": pid, "score": round(e["score"], 1),
                           "hits": [hit_to_dict(h) for h in e["hits"]]}
                          for pid, e in pages]}

    def expand_page_context(self, page_id: str) -> dict:
        """Cross-page contextual expansion: neighbour pages' first/last lines."""
        row = self.conn.execute(
            "SELECT case_short, page_number FROM pages WHERE page_id=?",
            (page_id,)).fetchone()
        if not row:
            return {}
        out = {}
        for delta, tag in ((-1, "prev_page"), (1, "next_page")):
            nb = self.conn.execute(
                "SELECT page_id FROM pages WHERE case_short=? AND page_number=?",
                (row["case_short"], row["page_number"] + delta)).fetchone()
            if nb:
                lines = self.conn.execute(
                    "SELECT line_id, line_number, source, text_raw, bbox, confidence "
                    "FROM lines WHERE page_id=? ORDER BY reading_order "
                    "LIMIT 8" if delta > 0 else
                    "SELECT line_id, line_number, source, text_raw, bbox, confidence "
                    "FROM lines WHERE page_id=? ORDER BY reading_order DESC LIMIT 8",
                    (nb["page_id"],)).fetchall()
                out[tag] = {"page_id": nb["page_id"],
                            "lines": [dict(r) for r in reversed(lines)]
                            if delta > 0 else [dict(r) for r in lines]}
        return out

    def log_run(self, query: str, profile: str | None, hits: list[Hit]) -> int:
        cur = self.conn.execute(
            "INSERT INTO search_runs(query, profile, params, created_at) "
            "VALUES(?,?,?,?)",
            (query, profile, json.dumps(self.w), time.strftime("%Y-%m-%dT%H:%M:%S%z")))
        run_id = cur.lastrowid
        self.conn.executemany(
            "INSERT INTO search_results(run_id, rank, line_id, score, "
            "components) VALUES(?,?,?,?,?)",
            [(run_id, i, h.line_id, h.total,
              json.dumps({"text": h.text_score, "context": h.context_score,
                          "layout": h.layout_score,
                          "variant": h.matched_variant}))
             for i, h in enumerate(hits)])
        self.conn.commit()
        return run_id


def hit_to_dict(h: Hit) -> dict:
    return {
        "line_id": h.line_id, "page_id": h.page_id,
        "line_number": h.line_number, "source": h.source,
        "text_raw": h.text_raw, "bbox": h.bbox, "confidence": h.confidence,
        "score": round(h.total, 1),
        "components": {"text": round(h.text_score, 1),
                        "context": round(h.context_score, 1),
                        "layout": round(h.layout_score, 1),
                        "matched_variant": h.matched_variant,
                        "note": "text=best partial_ratio over variants; "
                                "context=neighbour similarity; layout=position/"
                                "source points; OCR confidence only a capped hint"},
        "neighbors": h.neighbors,
        "review": dict(h.review) if h.review else None,
        "provisional_reference": h.provisional,
        "match_kind": h.match_kind,
        "evidence_class": h.evidence_class,
        "lead_status": h.lead_status,
    }
