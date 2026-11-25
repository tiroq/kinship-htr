#!/usr/bin/env python3
"""Live, read-only HTR dashboard. No recognition imports or worker restarts.

Shows completed atomic OCR checkpoints immediately, separately from the
batch-finalized SQLite index. All timestamps for HTR are checkpoint times.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from htr_config import load as load_config


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def epoch(value):
    try:
        return dt.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def clock(value):
    return dt.datetime.fromtimestamp(value).strftime("%H:%M:%S") if value is not None else "--:--:--"


def duration(value):
    if value is None:
        return "-"
    if value < 60:
        return f"{value:.1f}s"
    m, s = divmod(int(value), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def bar(done, total, width=24):
    fraction = min(1, max(0, done / total)) if total else 0
    n = int(fraction * width)
    return "[" + "#" * n + "-" * (width - n) + f"] {fraction:6.1%}"


def number(value):
    return f"{value:,}" if isinstance(value, int) else "-"


def process_stats(parent, child):
    ids = list(dict.fromkeys(str(p) for p in (parent, child)
                             if isinstance(p, int) and p > 0))
    if not ids:
        return {}
    try:
        result = subprocess.run(
            ["ps", "-o", "pid=,ppid=,pcpu=,rss=,command=", "-p", ",".join(ids)],
            capture_output=True, text=True, timeout=2)
        out = {}
        for line in result.stdout.splitlines():
            p, pp, cpu, rss, cmd = line.split(None, 4)
            out[int(p)] = {"ppid": int(pp), "cpu": float(cpu),
                           "rss_mib": int(rss) / 1024, "cmd": cmd}
        return out
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def db_snapshot(cfg):
    path = Path(cfg["db_path"]).resolve()
    # Never create a database and never run migrations from the monitor.
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.5) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        rows = {p: (status, lines or 0) for p, status, lines in conn.execute(
            "SELECT page_id,status,line_count FROM pages")}
        line_count = conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
        conn.commit()
    conn.close()
    return rows, line_count


class Checkpoints:
    """Cache metadata reads; inspect only atomic meta.json completion markers."""
    def __init__(self, root):
        self.root = Path(root)
        self.cache = {}

    def scan(self):
        seen = set()
        if not self.root.is_dir():
            self.cache.clear()
            return {}
        for folder in self.root.iterdir():
            if folder.name.startswith("_") or not folder.is_dir():
                continue
            path = folder / "meta.json"
            try:
                st = path.stat()
            except OSError:
                continue
            seen.add(path)
            signature = (st.st_mtime_ns, st.st_size)
            if path not in self.cache or self.cache[path][0] != signature:
                meta = read_json(path)
                if not isinstance(meta, dict) or meta.get("page_id") != folder.name:
                    self.cache.pop(path, None)
                    continue
                self.cache[path] = (signature, meta)
        self.cache = {p: v for p, v in self.cache.items() if p in seen}
        return {v[1]["page_id"]: v[1] for v in self.cache.values()
                if v[1].get("status") in ("ok", "failed")}


def inventory(cfg):
    state = read_json(cfg["state_path"])
    if not isinstance(state, dict) or not isinstance(state.get("files"), dict):
        raise ValueError("inventory state unavailable")
    records = list(state["files"].values())
    stable = {r["page_id"]: r for r in records if r.get("page_id")
              and r.get("confirmed_at") and r.get("decode_ok")}
    return records, stable


def classify(records, stable, rows, metas, max_attempts):
    """Disjoint stable-page buckets; indexed pages never counted twice."""
    sources = defaultdict(Counter)
    for r in records:
        sources[r.get("source_id", "?")]["discovered"] += 1
    for pid, r in stable.items():
        c = sources[r.get("source_id", "?")]
        c["stable"] += 1
        status = rows.get(pid, (None, 0))[0]
        checkpoint = metas.get(pid, {}).get("status")
        if status == "ok":
            c["indexed"] += 1
        elif status == "duplicate":
            c["duplicate"] += 1
        elif checkpoint == "ok":
            c["ocr_ready"] += 1
        elif r.get("parked") or (r.get("attempts") or 0) >= max_attempts:
            c["parked"] += 1
        elif checkpoint == "failed" or status == "failed":
            c["retry"] += 1
        else:
            c["pending"] += 1
    total = Counter()
    for c in sources.values():
        total.update(c)
    return sources, total


def read_events(path):
    events = []
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return events


def rates(events):
    # Use completed cycle wall times, not burst timestamps or inference-only time.
    samples = []
    last = None
    for e in events:
        event = e.get("event")
        if event == "daemon_started":
            last = None
        elif event == "round_finished":
            last = e
        elif event == "round_timing":
            if last and last.get("round_no") == e.get("round_no"):
                n = last.get("finalize", {}).get("indexed")
                seconds = e.get("loop_s")
                if isinstance(n, int) and n >= 0 and isinstance(seconds, (int, float)) and seconds > 0:
                    samples.append((n, seconds))
            last = None
    def rate(s):
        return 3600 * sum(n for n, _ in s) / sum(t for _, t in s) if s else None
    return rate(samples[-5:]), rate(samples)


def operations(metas, rows, stable, limit):
    items = []
    for pid, meta in metas.items():
        stamp = epoch(meta.get("completed_at"))
        if stamp is None:
            continue
        status = meta["status"]
        db_status = rows.get(pid, (None, 0))[0]
        state = ("INDEXED" if db_status == "ok" else "OCR OK") if status == "ok" else "FAILED"
        lines = meta.get("line_count")
        if isinstance(lines, int):
            lines += meta.get("extra_line_count") or 0
        items.append((stamp, pid, stable.get(pid, {}).get("source_id", "?"),
                      state, meta.get("elapsed_s"), lines,
                      meta.get("avg_confidence"), meta.get("error", "")))
    return sorted(items, reverse=True)[:limit]


def render(cfg, records, stable, rows, line_count, metas, events, tail,
           snapshot_age, warning=None):
    now = time.time()
    width = min(150, shutil.get_terminal_size((120, 40)).columns)
    sources, total = classify(records, stable, rows, metas, int(cfg["max_attempts"]))
    daemon = read_json(Path(cfg["heartbeat_dir"]) / "daemon.json") or {}
    child = read_json(Path(cfg["heartbeat_dir"]) / "w0.json") or {}
    try:
        parent_pid = int(Path(cfg["pid_path"]).read_text().strip())
    except (OSError, ValueError):
        parent_pid = None
    child_pid = child.get("pid")
    procs = process_stats(parent_pid, child_pid)
    parent = procs.get(parent_pid, {})
    alive = "htr_worker.py" in parent.get("cmd", "") and "daemon" in parent.get("cmd", "")
    worker = procs.get(child_pid, {})
    child_alive = alive and worker.get("ppid") == parent_pid
    phase = child.get("phase") if child_alive else None
    page = child.get("page_id") if child_alive else None
    age = max(0, now - child["updated_epoch"]) if child_alive and isinstance(child.get("updated_epoch"), (int, float)) else None
    daemon_age = max(0, now - daemon["updated_epoch"]) if isinstance(daemon.get("updated_epoch"), (int, float)) else None
    timeout = cfg.get("page_timeout_s", 900)
    status = ("STOPPED / PID not verified" if not alive else
              "STALE CHILD HEARTBEAT" if age is not None and age > timeout else
              "RECOGNIZING" if phase == "page" else
              "MODEL INIT" if phase == "init" else
              "CHECKPOINT SAVED" if phase == "ok" else
              "BETWEEN PAGES / FINALIZING" if daemon.get("phase") == "round" else
              "IDLE / DISCOVERY")
    good = total["indexed"] + total["duplicate"]
    recognized = good + total["ocr_ready"]
    remaining = total["pending"] + total["retry"]
    rate, historical = rates(events)
    eta = remaining * 3600 / rate if rate else None
    recent = operations(metas, rows, stable, tail)
    recent_durations = [m[4] for m in recent if m[3] in ("INDEXED", "OCR OK") and isinstance(m[4], (float, int))]
    average = sum(recent_durations) / len(recent_durations) if recent_durations else None
    round_start = daemon.get("updated_epoch")
    finished_in_round = sum(1 for m in metas.values()
                            if isinstance(round_start, (float, int))
                            and (epoch(m.get("completed_at")) or 0) >= int(round_start))
    # index/total is provided only on phase=page; do not guess after a respawn.
    out = [
        f"HTR LIVE MONITOR   {clock(now)}   {status}",
        "=" * width,
        f"CORPUS  {bar(recognized, total['discovered'])}  "
        f"{number(recognized)}/{number(total['discovered'])} recognized/resolved",
        f"Indexed OK {number(total['indexed'])} | aliases {number(total['duplicate'])} | "
        f"OCR ready, not indexed {number(total['ocr_ready'])} | lines in DB {number(line_count)}",
        f"Stable {number(total['stable'])} | pending {number(total['pending'])} | retry {number(total['retry'])} | "
        f"parked {number(total['parked'])} | unstable {number(total['discovered']-total['stable'])}",
        f"Rate last 5 completed rounds: {f'{rate:.1f}/h' if rate is not None else '-'} | "
        f"all timed rounds: {f'{historical:.1f}/h' if historical is not None else '-'} | "
        f"ETA known pending: {duration(eta)} (future downloads excluded)",
        f"Recent OCR avg: {duration(average)} | completed page checkpoints are visible before DB commit",
        "",
        "SOURCES          PROGRESS                     KNOWN    INDEXED   OCR READY   PENDING    RETRY/PARKED",
    ]
    for source, c in sorted(sources.items()):
        n = c["indexed"] + c["duplicate"] + c["ocr_ready"]
        out.append(f"{source:<16} {bar(n,c['discovered'],16)} "
                   f"{c['discovered']:>7,} {c['indexed']+c['duplicate']:>10,} "
                   f"{c['ocr_ready']:>11,} {c['pending']:>9,} {c['retry']:>6,}/{c['parked']:<6,}")
    out += ["", "CURRENT WORKER",
            f"Daemon {parent_pid or '-'} | heartbeat {duration(daemon_age)} (per round) | "
            f"child {child_pid if child_alive else '-'} | heartbeat {duration(age)}"]
    if child_alive:
        out.append(f"CPU {worker['cpu']:.1f}% | RSS {worker['rss_mib']:.0f} MiB | "
                   f"phase {phase or '-'} | page {page or '-'}")
        if phase == "page":
            idx, count = child.get("index"), child.get("total")
            out.append(f"Slice page {idx or '-'}/{count or '-'} | "
                       f"current page elapsed {duration(age)} | "
                       f"completed checkpoints this round {finished_in_round}")
    try:
        free = shutil.disk_usage(cfg["runtime_root"]).free / 2**30
        out.append(f"Runtime disk free {free:.1f} GiB | safety floor {cfg.get('min_free_gb', '?')} GiB")
    except OSError:
        out.append("Runtime disk unavailable")
    out += ["", "RECENT OPERATIONS  (actual HTR completion time, not batch index time)",
            "TIME      PAGE                         SOURCE          STATUS       TOTAL    LINES   CONF"]
    for stamp, pid, source, state, elapsed, lines, confidence, error in recent:
        conf = f"{confidence:.3f}" if isinstance(confidence, (int,float)) else "-"
        out.append(f"{clock(stamp):<9} {pid:<28} {source:<15} {state:<11} "
                   f"{duration(elapsed):>8} {number(lines):>7} {conf:>6}"
                   + (f"  {str(error)[:80]}" if error else ""))
    errors = [e for e in events if e.get("event") in
              ("page_failed", "daemon_error", "finalize_error", "page_timeout", "dedup_error")]
    if errors:
        e = errors[-1]
        out.append(f"Last logged error: {e.get('ts','')} {e.get('page_id','')} {str(e.get('error',e.get('event')))[:90]}")
    out.append(f"DB/inventory snapshot age {snapshot_age:.1f}s | Ctrl+C closes monitor only")
    if warning:
        out.append(f"SNAPSHOT WARNING: {warning}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--tail", type=int, default=12)
    ap.add_argument("--once", action="store_true")
    ns = ap.parse_args()
    if ns.interval <= 0 or ns.tail < 1:
        ap.error("interval and tail must be positive")
    cfg = load_config(ns.config)
    checkpoints = Checkpoints(cfg["ocr_root"])
    records, stable, rows, line_count, events, metas = [], {}, {}, 0, [], {}
    refreshed = scanned = float("-inf")
    warning = None
    try:
        while True:
            tick = time.monotonic()
            if tick - refreshed >= 5:
                try:
                    new_records, new_stable = inventory(cfg)
                    new_rows, new_lines = db_snapshot(cfg)
                    records, stable, rows, line_count = new_records, new_stable, new_rows, new_lines
                    events = read_events(cfg["progress_log"])
                    refreshed = tick
                    warning = None
                except (OSError, ValueError, sqlite3.Error) as e:
                    warning = f"{type(e).__name__}: {e}"
            if tick - scanned >= 2:
                try:
                    metas = checkpoints.scan()
                    scanned = tick
                except OSError as e:
                    warning = f"checkpoint scan: {e}"
            screen = render(cfg, records, stable, rows, line_count, metas,
                            events, ns.tail, tick - refreshed, warning)
            if sys.stdout.isatty() and not ns.once:
                sys.stdout.write("\033[H\033[J")
            print(screen, flush=True)
            if ns.once:
                return 0
            time.sleep(max(0.05, ns.interval - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
