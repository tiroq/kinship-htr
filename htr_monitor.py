#!/usr/bin/env python3
"""Read-only operational monitor for the incremental HTR indexer.

Reports discovery/processing/index metrics, throughput (measured, with the
exclusion of unknown totals spelled out), ETA over KNOWN pending pages only,
resource usage (disk, worker RSS/CPU, memory pressure, swap) and the latest
operations/errors.  Never writes to the database, state, or worker files.

Usage:  python htr_monitor.py [--json] [--tail N]
        python htr_worker.py status        (same report)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from htr_config import load as load_config  # noqa: E402


def _read_json(path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _db_stats(cfg) -> dict:
    from htr_search.db import connect
    out = {}
    try:
        conn = connect(cfg["db_path"], readonly=True)
        for key, sql in (
                ("pages", "SELECT COUNT(*) FROM pages"),
                ("pages_ok", "SELECT COUNT(*) FROM pages WHERE status='ok'"),
                ("pages_failed",
                 "SELECT COUNT(*) FROM pages WHERE status='failed'"),
                ("pages_duplicate",
                 "SELECT COUNT(*) FROM pages WHERE status='duplicate'"),
                ("lines", "SELECT COUNT(*) FROM lines"),
                ("reviews", "SELECT COUNT(*) FROM reviews"),
                ("confirmed_reviews",
                 "SELECT COUNT(*) FROM reviews WHERE decision='confirmed'")):
            out[key] = conn.execute(sql).fetchone()[0]
        out["by_collection"] = {
            r[0]: r[1] for r in conn.execute(
                "SELECT COALESCE(collection,'?'), COUNT(*) FROM pages "
                "GROUP BY collection ORDER BY 2 DESC")}
        out["lines_by_collection"] = {
            r[0]: r[1] for r in conn.execute(
                "SELECT COALESCE(p.collection,'?'), COUNT(*) FROM lines l "
                "JOIN pages p USING(page_id) GROUP BY p.collection")}
        try:
            out["origin"] = {
                r[0]: r[1] for r in conn.execute(
                    "SELECT json_extract(provenance,'$.origin'), COUNT(*) "
                    "FROM pages GROUP BY 1")}
        except Exception:
            out["origin"] = {}
        conn.close()
    except Exception as e:
        out["error"] = repr(e)
    return out


def _progress_stats(cfg, tail: int = 15) -> dict:
    p = Path(cfg["progress_log"])
    out = {"events": 0, "recent_pages_per_hour": None,
           "avg_pages_per_hour_all": None, "avg_page_duration_s": None,
           "recent": [], "errors": []}
    if not p.exists():
        return out
    events = []
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        return out
    out["events"] = len(events)
    out["recent"] = events[-tail:]
    done = [e for e in events if e.get("event") == "page_indexed"]
    out["errors"] = [e for e in events
                     if e.get("event") in ("page_failed", "daemon_error",
                                           "finalize_error", "page_timeout",
                                           "dedup_error")]
    out["errors"] = out["errors"][-tail:]

    def parse_ts(ts: str) -> float:
        import datetime
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.datetime.strptime(ts, fmt).timestamp()
            except (ValueError, TypeError):
                continue
        return 0.0

    if done:
        durs = [e["elapsed_s"] for e in done
                if isinstance(e.get("elapsed_s"), (int, float))]
        if durs:
            out["avg_page_duration_s"] = round(sum(durs) / len(durs), 1)
        now = time.time()
        recent = [e for e in done
                  if now - parse_ts(e["ts"]) <= 3600]
        out["recent_pages_per_hour"] = len(recent)
        t_first = min(parse_ts(e["ts"]) for e in done)
        t_last = max(parse_ts(e["ts"]) for e in done)
        span_h = max((t_last - t_first) / 3600.0, 1 / 60.0)
        out["avg_pages_per_hour_all"] = round(len(done) / span_h, 2)
        out["total_indexed_events"] = len(done)
    return out


def _pending(cfg) -> dict:
    """Known pending = stable discovered images not yet ok/duplicate in DB."""
    try:
        from htr_inventory import load_state, stable_files
        from htr_worker import db_conn
        state = load_state(cfg)
        stable = stable_files(cfg)
        parked = sum(1 for r in state["files"].values()
                     if r.get("parked") or (r.get("attempts") or 0) >=
                     int(cfg["max_attempts"]))
        conn = db_conn(cfg, readonly=True)
        done = {r[0] for r in conn.execute(
            "SELECT page_id FROM pages WHERE status IN ('ok','duplicate')")}
        failed = {r[0] for r in conn.execute(
            "SELECT page_id FROM pages WHERE status='failed'")}
        conn.close()
        pending = [r for r in stable
                   if r["page_id"] not in done and r["page_id"] not in failed]
        failed_known = [r for r in stable if r["page_id"] in failed]
        return {"discovered_files": len(state["files"]),
                "stable": len(stable), "pending": len(pending),
                "failed_known": len(failed_known), "parked": parked,
                "pending_by_source": _count_by(pending, "source_id"),
                "stable_by_source": _count_by(stable, "source_id")}
    except Exception as e:
        return {"error": repr(e)}


def _count_by(records: list, key: str) -> dict:
    out: dict[str, int] = {}
    for r in records:
        k = r.get(key) or "?"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def _worker(cfg) -> dict:
    out = {"pid": None, "alive": False, "heartbeat_age_s": None,
           "phase": None, "page_id": None, "rss_mb": None, "cpu_pct": None}
    hb = _read_json(Path(cfg["heartbeat_dir"]) / "daemon.json")
    pid = None
    pidf = Path(cfg["pid_path"])
    if pidf.exists():
        try:
            pid = int(pidf.read_text().strip())
        except ValueError:
            pass
    if hb:
        out["phase"] = hb.get("phase")
        out["queue"] = hb.get("queue")
        out["heartbeat_age_s"] = round(time.time() -
                                       hb.get("updated_epoch", 0), 1)
        pid = pid or hb.get("pid")
    child = _read_json(Path(cfg["heartbeat_dir"]) / "w0.json")
    if child:
        out["current_page"] = child.get("page_id")
        out["child_phase"] = child.get("phase")
        out["child_heartbeat_age_s"] = round(time.time() -
                                             child.get("updated_epoch", 0), 1)
    if pid:
        out["pid"] = pid
        try:
            ps = subprocess.run(
                ["ps", "-o", "rss=,pcpu=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5)
            if ps.stdout.strip():
                rss, cpu = ps.stdout.split()[:2]
                out["rss_mb"] = round(int(rss) / 1024, 1)
                out["cpu_pct"] = float(cpu)
                out["alive"] = True
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return out


def _disk(cfg) -> dict:
    out = {}
    roots = {"runtime": cfg["runtime_root"]}
    try:
        from htr_inventory import load_state
        for src in load_config()["sources"]:
            roots[src["id"]] = src["root"]
    except Exception:
        pass
    for name, root in roots.items():
        try:
            if not Path(root).exists():
                out[name] = "missing"
                continue
            st = os.statvfs(root)
            avail = st.f_bavail * st.f_frsize / 2**30
            total = (st.f_blocks - st.f_bfree) * st.f_frsize / 2**30 + avail
            out[name] = {"avail_gb": round(avail, 1),
                         "used_gb": round(total - avail, 1)}
        except OSError:
            out[name] = "unavailable"
    return out


def _system() -> dict:
    out = {}
    try:
        r = subprocess.run(["sysctl", "-n", "vm.swapusage"],
                           capture_output=True, text=True, timeout=5)
        out["swap"] = r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out["swap"] = "?"
    try:
        r = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
        pages = {}
        for line in r.stdout.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                pages[k.strip()] = v.strip().rstrip(".")
        def num(key):
            try:
                return int(pages.get(key, "0").replace(" pages", "")) * 4096
            except ValueError:
                return 0
        free = num("Pages free") + num("Pages inactive")
        total = free
        for k in ("Pages active", "Pages wired down", "Pages occupied by "
                  "compressor", "Pages speculative"):
            total += num(k)
        if total:
            out["mem_free_pct"] = round(100.0 * free / total, 1)
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def _sidecars(cfg) -> dict:
    root = Path(cfg["sidecar_root"])
    if not root.exists():
        return {"count": 0}
    n = sum(1 for _ in root.rglob("*.htr.json")
            if "revisions" not in _.parts)
    return {"count": n}


def report(cfg: dict, as_json: bool = False, tail: int = 15) -> dict:
    db = _db_stats(cfg)
    prog = _progress_stats(cfg, tail)
    pend = _pending(cfg)
    worker = _worker(cfg)
    disk = _disk(cfg)
    system = _system()
    sidecars = _sidecars(cfg)

    # ETA over KNOWN pending only (unknown/discovered-later totals excluded)
    rate = prog.get("recent_pages_per_hour") or prog.get("avg_pages_per_hour_all")
    eta = None
    if rate and rate > 0 and isinstance(pend.get("pending"), int):
        eta = round(pend["pending"] / rate, 1)

    out = {
        "discovered": pend.get("discovered_files"),
        "stable": pend.get("stable"),
        "pending_known": pend.get("pending"),
        "pending_by_source": pend.get("pending_by_source"),
        "failed": db.get("pages_failed"),
        "failed_known": pend.get("failed_known"),
        "parked": pend.get("parked"),
        "duplicates_skipped": db.get("pages_duplicate"),
        "pages_indexed": db.get("pages_ok"),
        "pages_total_db": db.get("pages"),
        "imported_vs_new": db.get("origin"),
        "by_collection": db.get("by_collection"),
        "indexed_lines": db.get("lines"),
        "lines_by_collection": db.get("lines_by_collection"),
        "reviews": db.get("reviews"),
        "reviews_confirmed": db.get("confirmed_reviews"),
        "sidecars": sidecars.get("count"),
        "recent_pages_per_hour": prog.get("recent_pages_per_hour"),
        "avg_pages_per_hour_measured": prog.get("avg_pages_per_hour_all"),
        "avg_page_duration_s": prog.get("avg_page_duration_s"),
        "eta_hours_known_pending": eta,
        "eta_note": ("ETA covers KNOWN stable pending images only; "
                     "GAVO Watchdog is still downloading new scans — "
                     "undiscovered totals are excluded"),
        "throughput_note": ("measured throughput from page_indexed events; "
                            "theoretical capacity ~3600/avg_page_duration "
                            "pages/hour at 1 thread"),
        "worker": worker,
        "disk": disk,
        "system": system,
        "recent_operations": prog.get("recent"),
        "recent_errors": prog.get("errors"),
        "db_error": db.get("error"),
    }
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return out

    def line(k, v):
        print(f"  {k:<34} {v}")

    print("HTR indexer status (read-only)")
    print("Discovery")
    line("discovered images", out["discovered"])
    line("stable (ready)", out["stable"])
    line("pending (known)", out["pending_known"])
    line("pending by source", out["pending_by_source"] or {})
    print("Processing")
    line("indexed pages (ok)", f"{out['pages_indexed']} / {out['pages_total_db']}")
    line("imported vs new", out["imported_vs_new"] or {})
    line("duplicates skipped", out["duplicates_skipped"])
    line("failed / parked", f"{out['failed']} / {out['parked']}")
    line("portable sidecars", out["sidecars"])
    print("Index")
    line("indexed lines", out["indexed_lines"])
    line("lines by collection", out["lines_by_collection"] or {})
    line("reviews (confirmed)", f"{out['reviews']} ({out['reviews_confirmed']})")
    print("Throughput (measured)")
    line("recent pages/hour", out["recent_pages_per_hour"])
    line("historical avg pages/hour", out["avg_pages_per_hour_measured"])
    line("avg page duration (s)", out["avg_page_duration_s"])
    line("ETA (known pending, h)", out["eta_hours_known_pending"])
    line("note", out["eta_note"])
    print("Worker")
    for k in ("pid", "alive", "phase", "current_page", "heartbeat_age_s",
              "child_heartbeat_age_s", "rss_mb", "cpu_pct"):
        if out["worker"].get(k) is not None:
            line(k, out["worker"][k])
    print("Disk / system")
    for k, v in out["disk"].items():
        line(k, v)
    for k, v in out["system"].items():
        line(k, v)
    if out["recent_errors"]:
        print("Recent errors")
        for e in out["recent_errors"][-5:]:
            line(e.get("event"), f"{e.get('ts')} {e.get('page_id', '')} "
                                 f"{str(e.get('error', ''))[:90]}")
    if out["db_error"]:
        line("DB ERROR", out["db_error"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--tail", type=int, default=15)
    ap.add_argument("--config", default=None)
    ns = ap.parse_args()
    cfg = load_config(ns.config)
    report(cfg, as_json=ns.json, tail=ns.tail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
