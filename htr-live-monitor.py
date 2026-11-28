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
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from htr_config import load as load_config

RESET = "\033[0m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
ORANGE = "\033[38;5;208m"
RED = "\033[91m"
CYAN = "\033[96m"
GRAY = "\033[90m"
BOLD = "\033[1m"

COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def paint(value, color):
    return f"{color}{value}{RESET}" if COLOR else str(value)


def state_color(state):
    if state in ("INDEXED", "ALIAS", "CHECKPOINT SAVED"):
        return GREEN
    if state in ("WAIT DB", "RECOGNIZING"):
        return CYAN
    if state in ("IDLE / DISCOVERY",):
        return GRAY
    if state == "FAILED" or state.startswith(("STOPPED", "STALE")):
        return RED
    return YELLOW


def elapsed_color(seconds, peak):
    # Same relative time scale as GAVO Queue Monitor.
    if not isinstance(seconds, (int, float)) or peak <= 0:
        return GRAY
    ratio = seconds / peak
    return GREEN if ratio < .25 else YELLOW if ratio < .5 else ORANGE if ratio < .75 else RED


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
    return paint("█" * n, GREEN) + paint("░" * (width - n), GRAY) + f" {fraction:6.1%}"


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
        state = ("INDEXED" if db_status == "ok" else "ALIAS" if db_status == "duplicate" else "WAIT DB") if status == "ok" else "FAILED"
        lines = meta.get("line_count")
        if isinstance(lines, int):
            lines += meta.get("extra_line_count") or 0
        items.append((stamp, pid, stable.get(pid, {}).get("source_id", "?"),
                      state, meta.get("elapsed_s"), lines,
                      meta.get("avg_confidence"), meta.get("error", "")))
    return sorted(items, reverse=True)[:limit]


def pipeline_counts(total, page, stable, rows, metas, max_attempts):
    """Separate the verified active page from the waiting queue."""
    pending, retry = total["pending"], total["retry"]
    active = 0
    rec = stable.get(page) if page else None
    if rec and rows.get(page, (None, 0))[0] not in ("ok", "duplicate") and metas.get(page, {}).get("status") != "ok":
        if not rec.get("parked") and (rec.get("attempts") or 0) < max_attempts:
            if metas.get(page, {}).get("status") == "failed" or rows.get(page, (None, 0))[0] == "failed":
                retry -= 1
            else:
                pending -= 1
            active = 1
    return pending, retry, active


def fit(line, width):
    plain = re.sub(r"\x1b\[[0-9;]*m", "", line)
    return line if len(plain) <= width else plain[:max(0, width - 1)] + "…"


def render(cfg, records, stable, rows, line_count, metas, events, tail,
           snapshot_age, warning=None):
    now = time.time()
    columns, height = shutil.get_terminal_size((120, 40))
    width = min(150, max(1, columns - 1))
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
    last_round = next((e for e in reversed(events) if e.get("event") == "round_finished"), {})
    status = ("STOPPED / PID not verified" if not alive else
              "STALE CHILD HEARTBEAT" if age is not None and age > timeout else
              "RECOGNIZING" if phase == "page" else
              "MODEL INIT" if phase == "init" else
              "CHECKPOINT SAVED" if phase == "ok" else
              "BETWEEN SLICES / POSSIBLE FINALIZE" if daemon.get("phase") == "round" else
              "IDLE / DISCOVERY")
    good = total["indexed"] + total["duplicate"]
    recognized = good + total["ocr_ready"]
    remaining = total["pending"] + total["retry"]
    pending, retry, active = pipeline_counts(total, page if status == "RECOGNIZING" else None, stable, rows, metas, int(cfg["max_attempts"]))
    rate, historical = rates(events)
    eta = remaining * 3600 / rate if rate else None
    recent = operations(metas, rows, stable, tail)
    recent_durations = [m[4] for m in recent if m[3] in ("INDEXED", "WAIT DB", "ALIAS") and isinstance(m[4], (float, int))]
    average = sum(recent_durations) / len(recent_durations) if recent_durations else None
    peak = max(recent_durations, default=0)
    round_start = daemon.get("updated_epoch")
    finished_in_round = sum(1 for m in metas.values()
                            if isinstance(round_start, (float, int))
                            and (epoch(m.get("completed_at")) or 0) >= int(round_start))
    # index/total is provided only on phase=page; do not guess after a respawn.
    out = [
        paint("HTR LIVE MONITOR", BOLD + CYAN) + f"   {clock(now)}   " + paint(status, state_color(status)),
        "=" * width,
        f"OCR / RESOLVED {bar(recognized, total['discovered'], min(24, max(4, width-55)))}  {number(recognized)}/{number(total['discovered'])}",
        f"DB  / RESOLVED {bar(good, total['discovered'], min(24, max(4, width-55)))}  {number(good)}/{number(total['discovered'])}",
        paint("PIPELINE (pages)", BOLD + CYAN),
        f"01 DISCOVERY     {number(total['discovered'])} found | {number(total['discovered']-total['stable'])} not stable",
        f"02 WAIT OCR      {paint(number(pending), GRAY)} queued | {paint(number(retry), YELLOW)} retry",
        f"03 RECOGNIZING   {paint(number(active), CYAN)} active (verified pending page)",
        f"04 WAIT DB       {paint(number(total['ocr_ready']), CYAN)} OCR saved, awaiting index",
        f"05 INDEXED       {paint(number(total['indexed']), GREEN)} searchable | {number(total['duplicate'])} aliases | {number(line_count)} DB lines",
        f"!! PARKED        {paint(number(total['parked']), RED if total['parked'] else GREEN)} need review",
        f"Rate: {f'{rate:.1f}/h' if rate is not None else '-'} last 5 rounds | ETA OCR queue: {paint(duration(eta), CYAN)} (known only)",
        f"All rounds: {f'{historical:.1f}/h' if historical is not None else '-'} | Recent OCR avg: {duration(average)}",
        "-" * width,
        "SOURCES          PROGRESS                     KNOWN    INDEXED     WAIT DB   PENDING    RETRY/PARKED",
    ]
    for source, c in sorted(sources.items()):
        n = c["indexed"] + c["duplicate"] + c["ocr_ready"]
        out.append(f"{source:<16} {bar(n,c['discovered'],16)} "
                   f"{c['discovered']:>7,} {c['indexed']+c['duplicate']:>10,} "
                   f"{c['ocr_ready']:>11,} {c['pending']:>9,} {c['retry']:>6,}/{c['parked']:<6,}")
    out += ["-" * width, paint("CURRENT WORKER", BOLD + CYAN),
            f"Daemon {parent_pid or '-'} | heartbeat {duration(daemon_age)} (per round) | "
            f"child {child_pid if child_alive else '-'} | heartbeat {duration(age)}"]
    if child_alive:
        out.append(f"CPU {worker['cpu']:.1f}% | RSS {worker['rss_mib']:.0f} MiB | "
                   f"phase {paint(phase or '-', state_color(status))} | page {paint(page or '-', CYAN)}")
        if phase == "page":
            idx, count = child.get("index"), child.get("total")
            position = f"{idx or '-'}/{count or '-'}"
            out.append(f"Slice page {paint(position, CYAN)} | "
                       f"current page elapsed {paint(duration(age), elapsed_color(age, peak))} | "
                       f"checkpoints since round start {paint(finished_in_round, GREEN)}")
    if last_round:
        fin = last_round.get("finalize", {})
        out.append(f"Last completed batch #{last_round.get('round_no', '?')}: "
                   f"{paint(fin.get('indexed', 0), GREEN)} indexed | {paint(fin.get('failed', 0), RED if fin.get('failed') else GREEN)} failed")
    try:
        free = shutil.disk_usage(cfg["runtime_root"]).free / 2**30
        floor = cfg.get("min_free_gb", 5)
        disk_color = RED if free < floor else YELLOW if free < 2 * floor else GREEN
        out.append(f"Runtime disk free {paint(f'{free:.1f} GiB', disk_color)} | safety floor {floor} GiB")
    except OSError:
        out.append("Runtime disk unavailable")
    out += ["-" * width, paint("RECENT OPERATIONS", BOLD + CYAN) + "  (OCR completion time; STATUS = current DB state)",
            "TIME      PAGE                         SOURCE          STATUS       TOTAL    LINES   CONF"]
    # Keep the pipeline and worker details; shrink recent history.
    visible_recent = max(1, height - len(out) - 5)
    for stamp, pid, source, state, elapsed, lines, confidence, error in recent[:visible_recent]:
        conf = f"{confidence:.3f}" if isinstance(confidence, (int,float)) else "-"
        out.append(f"{clock(stamp):<9} {pid:<28} {source:<15} {paint(f'{state:<11}', state_color(state))} "
                   f"{paint(f'{duration(elapsed):>8}', elapsed_color(elapsed, peak))} {number(lines):>7} {conf:>6}"
                   + (f"  {str(error)[:80]}" if error else ""))
    if peak > 0:
        out.append("TIME SCALE (recent pages): " + " | ".join((
            paint(f"0-{peak*.25:.1f}s", GREEN),
            paint(f"{peak*.25:.1f}-{peak*.5:.1f}s", YELLOW),
            paint(f"{peak*.5:.1f}-{peak*.75:.1f}s", ORANGE),
            paint(f"{peak*.75:.1f}s+", RED))))
    errors = [e for e in events if e.get("event") in
              ("page_failed", "daemon_error", "finalize_error", "page_timeout", "dedup_error")]
    if errors:
        e = errors[-1]
        out.append(paint(f"Last logged error: {e.get('ts','')} {e.get('page_id','')} {str(e.get('error',e.get('event')))[:90]}", RED))
    out.append(f"DB/inventory snapshot age {snapshot_age:.1f}s | Ctrl+C closes monitor only")
    if warning:
        out.append(paint(f"SNAPSHOT WARNING: {warning}", YELLOW))
    return "\n".join(fit(line, width) for line in out)


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
