#!/usr/bin/env python3
"""Incremental, resumable local HTR indexing worker.

Standalone from GAVO Watchdog: discovers stable images (60 s filesystem
polling), imports existing recognition artifacts, recognizes pending pages
with the validated polyscriptor/kraken-blla/CRNN-CTC stack (1 worker,
1 thread, reduced priority), writes portable sidecars, and upserts the
unified SQLite index transactionally.

Subcommands:
  inventory   one discovery pass (read-only towards sources)
  import      legacy artifacts -> sidecars (+ reconcile into the DB)
  build       rebuild the DB from sidecars (no HTR)
  process     one processing round (poll + import + recognize + index)
  daemon      continuous worker (lock-guarded, heartbeat, graceful SIGTERM)
  status      read-only progress/statistics (alias: htr_monitor.py)
  stop        graceful shutdown of the running daemon
  retry       reset failed/parked pages so they re-enter the queue
  overlay     draw sidecar bboxes onto an original image (verification aid)

Idempotency: a page is done when its sidecar exists and the DB row is
ok/duplicate; restarts never repeat completed recognition.  Sidecar writes
are atomic; DB upserts are transactional (WAL) — a crash between them is
reconciled automatically on the next round.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import multiprocessing as mp
import os
import signal
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from htr_config import ensure_runtime_dirs, load as load_config  # noqa: E402

STOP = mp.Event()          # set by SIGTERM handler in the daemon parent
_lock_fh = None


# ---------------------------------------------------------------- logging

def log_event(cfg: dict, event: str, **kw) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event}
    rec.update(kw)
    p = Path(cfg["progress_log"])
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _log(*args) -> None:
    print(time.strftime("%H:%M:%S"), *args, flush=True)


# ---------------------------------------------------------------- singleton

def acquire_lock(cfg: dict):
    """Non-blocking exclusive lock — prevents duplicate worker instances."""
    global _lock_fh
    ensure_runtime_dirs(cfg)
    fh = open(cfg["lock_path"], "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SystemExit(f"another worker holds {cfg['lock_path']} "
                         "(duplicate instance prevented)")
    fh.seek(0), fh.truncate()
    fh.write(str(os.getpid())), fh.flush()
    _lock_fh = fh
    return fh


def release_lock() -> None:
    global _lock_fh
    if _lock_fh is not None:
        try:
            fcntl.flock(_lock_fh, fcntl.LOCK_UN)
        except OSError:
            pass
        _lock_fh.close()
        _lock_fh = None


def write_pid(cfg: dict) -> None:
    Path(cfg["pid_path"]).write_text(str(os.getpid()))


def read_pid(cfg: dict) -> int | None:
    p = Path(cfg["pid_path"])
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except ValueError:
        return None


# ---------------------------------------------------------------- sidecars/DB

def sidecar_target(cfg: dict, rec: dict) -> Path:
    from htr_sidecar import sidecar_path_for
    return sidecar_path_for(Path(rec["path"]), cfg, rec)


def _dims_map(cfg: dict) -> dict:
    dims = {}
    try:
        for pid, (_p, _sha, w, h) in json.loads(
                Path(cfg["legacy_manifest"]).read_text(encoding="utf-8")).items():
            dims[pid] = (w, h)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return dims


def build_sidecar_for(cfg: dict, rec: dict, origin: str) -> dict | None:
    """Sidecar for a recognized page (artifacts under legacy or runtime OCR root)."""
    from htr_import import page_dir_to_sidecar
    pid = rec["page_id"]
    for root, org in ((cfg["ocr_root"], "new"),
                      (cfg["legacy_ocr_root"], "imported")):
        page_dir = Path(root) / pid
        if (page_dir / "meta.json").exists():
            return page_dir_to_sidecar(page_dir, cfg, _dims_map(cfg),
                                       origin=origin or org, inv_rec=rec)
    return None


def db_conn(cfg: dict, readonly: bool = False):
    from htr_search.db import connect
    return connect(cfg["db_path"], readonly=readonly)


def db_canonical_map(conn) -> dict:
    """(sha256, config_fingerprint) -> page_id for every valid canonical
    recognition result (status ok WITH lines).

    Dedup requires BOTH image content and recognition configuration to match:
    the same scan recognized under a different model/patch is NOT a
    duplicate.  Fingerprints are recomputed from stored provenance with the
    normalized patch so legacy and new artifacts compare correctly; rows
    whose provenance lacks the keys yield no fingerprint and never match.
    """
    from htr_sidecar import fingerprint_from_provenance
    out: dict[tuple[str, str], str] = {}
    try:
        for row in conn.execute(
                "SELECT page_id, image_sha256, provenance FROM pages "
                "WHERE status='ok' AND image_sha256 IS NOT NULL "
                "AND image_sha256!='' AND COALESCE(line_count,0)>0"):
            try:
                prov = json.loads(row["provenance"] or "{}")
            except json.JSONDecodeError:
                continue
            fp = fingerprint_from_provenance(prov)
            if not fp:
                continue
            out.setdefault((row["image_sha256"], fp), row["page_id"])
    except Exception:
        pass                     # caller reports queue_db_error if this matters
    return out


def current_recognition_fingerprint() -> str:
    from htr_sidecar import config_fingerprint
    from stage_a_runner import RECOGNITION_IDENTITY
    return config_fingerprint(RECOGNITION_IDENTITY)


def resolve_canonical(conn, page_id: str, *, max_hops: int = 8) -> str | None:
    """Follow duplicate aliases until a VALID canonical result: status ok,
    lines present, no further alias.  Returns None for chains that dead-end
    (duplicate->duplicate with no ok target, cycles, missing pages) — such
    pages must be recognized instead of deduplicated."""
    seen: set[str] = set()
    cur = page_id
    for _ in range(max_hops):
        if cur in seen:
            return None
        seen.add(cur)
        row = conn.execute(
            "SELECT status, canonical_page_id, line_count FROM pages "
            "WHERE page_id=?", (cur,)).fetchone()
        if row is None:
            return None
        if (row["status"] == "ok" and not row["canonical_page_id"]
                and (row["line_count"] or 0) > 0):
            return cur
        nxt = row["canonical_page_id"]
        if not nxt:
            return None
        cur = nxt
    return None


def db_page_status(cfg: dict, page_id: str) -> str | None:
    try:
        conn = db_conn(cfg, readonly=True)
        row = conn.execute("SELECT status FROM pages WHERE page_id=?",
                           (page_id,)).fetchone()
        conn.close()
        return row["status"] if row else None
    except Exception:
        return None


def index_sidecar(cfg: dict, sidecar: dict) -> dict:
    from htr_search.db import upsert_page
    return upsert_page(cfg["db_path"], sidecar)


def write_duplicate_sidecar(cfg: dict, rec: dict, canonical: str) -> Path:
    """Record a content duplicate WITHOUT recognition (no inference repeat)."""
    from htr_sidecar import build_sidecar, validate_sidecar, write_sidecar
    source = _source_from_rec(rec)
    sidecar = build_sidecar(
        source=source,
        image={"width": rec.get("width"), "height": rec.get("height")},
        recognition={
            "engine": "none", "model": "none", "segmentation": "none",
            "segmentation_method": None, "model_preset": None,
            "device": None, "recognized_at": None, "duration_s": None,
            "status": "duplicate", "origin": "deduplicated",
            "error": f"content identical to {canonical}; inference skipped",
            "revision": 1,
        },
        lines=[],
        content={"sha256": rec.get("sha256"), "canonical_page_id": canonical,
                 "duplicate_of": canonical},
    )
    if validate_sidecar(sidecar):
        raise RuntimeError(f"invalid duplicate sidecar: "
                           f"{validate_sidecar(sidecar)}")
    target = sidecar_target(cfg, rec)
    write_sidecar(target, sidecar)
    return target


def _source_from_rec(rec: dict) -> dict:
    return {
        "source_ref": rec.get("case_ref") or rec.get("source_id") or "unknown",
        "page_id": rec["page_id"],
        "case_ref": rec.get("case_ref") or rec.get("source_id") or "unknown",
        "case_short": rec.get("case_short") or rec["page_id"],
        "page_number": int(rec.get("page_number")
                           if rec.get("page_number") is not None
                           else (rec.get("scan_number") or 0)),
        "original_filename": rec.get("original_filename") or "",
        "logical_path": rec.get("logical_path") or rec["page_id"],
        "image_path": rec["path"],
        "sha256": rec.get("sha256"),
        "size_bytes": rec.get("size"),
        "collection": rec.get("collection"),
        "archive": rec.get("archive"),
        "fond": rec.get("fond"), "opis": rec.get("opis"),
        "delo": rec.get("delo"),
        "scan_number": rec.get("scan_number"),
        "source_id": rec.get("source_id"),
        "root": rec.get("root"),
    }


def reconcile(cfg: dict) -> dict:
    """Heal crashes between artifacts / sidecar / DB (idempotent)."""
    from htr_inventory import stable_files
    from htr_sidecar import read_sidecar, validate_sidecar
    stats = {"sidecars_written": 0, "db_upserts": 0, "pending": 0,
             "warnings": []}
    for rec in stable_files(cfg):
        pid = rec["page_id"]
        target = sidecar_target(cfg, rec)
        db_status = db_page_status(cfg, pid)
        if not target.exists():
            sc = build_sidecar_for(cfg, rec, origin="")
            if sc is not None:
                from htr_sidecar import write_sidecar
                write_sidecar(target, sc)
                stats["sidecars_written"] += 1
            elif db_status == "failed":
                stats["warnings"].append(
                    f"{pid}: failed with no artifacts retained")
            else:
                stats["pending"] += 1
            continue
        if db_status is None:
            try:
                sc = read_sidecar(target)
                if validate_sidecar(sc):
                    stats["warnings"].append(
                        f"{pid}: invalid sidecar {validate_sidecar(sc)[:2]}")
                    continue
                index_sidecar(cfg, sc)
                stats["db_upserts"] += 1
            except Exception as e:
                stats["warnings"].append(f"{pid}: reconcile failed: {e!r}")
    return stats


# ---------------------------------------------------------------- queue

def _source_priority(cfg: dict) -> dict:
    return {s["id"]: int(s.get("priority", 50)) for s in cfg["sources"]}


def build_queue(cfg: dict, limit: int = 0) -> tuple[list[dict], dict]:
    """Fair pending queue: per-source round-robin (priority-ordered sources),
    newly discovered images first within their source, then page order."""
    from htr_inventory import load_state, stable_files
    state = load_state(cfg)
    files = state["files"]
    prio = _source_priority(cfg)
    now = time.time()
    stats = {"stable": 0, "done": 0, "duplicate": 0, "parked": 0,
             "failed_retry": 0, "pending": 0}

    # batched DB reads (one pass instead of thousands of queries/round)
    db_status_map: dict[str, str] = {}
    dedup_map: dict = {}
    conn = None
    try:
        conn = db_conn(cfg, readonly=True)
        for row in conn.execute("SELECT page_id, status FROM pages"):
            db_status_map[row["page_id"]] = row["status"]
        dedup_map = db_canonical_map(conn)
    except Exception as e:
        log_event(cfg, "queue_db_error", error=repr(e))
    try:
        current_fp = current_recognition_fingerprint()
    except Exception:
        current_fp = None

    by_source: dict[str, list[dict]] = {}
    for rec in stable_files(cfg):
        stats["stable"] += 1
        full = files.get(rec["path"], rec)
        attempts = int(full.get("attempts") or 0)
        parked = bool(full.get("parked"))
        pid = rec["page_id"]
        db_status = db_status_map.get(pid)
        target = sidecar_target(cfg, rec)

        if db_status in ("ok", "duplicate") and target.exists():
            stats["done" if db_status == "ok" else "duplicate"] += 1
            continue
        if parked or attempts >= int(cfg["max_attempts"]):
            stats["parked"] += 1
            continue
        if db_status == "failed":
            # retry: clear ONLY the runtime failure marker (never the
            # historical legacy artifacts) so worker_main re-runs the page
            mp_ = Path(cfg["ocr_root"]) / pid / "meta.json"
            if mp_.exists():
                try:
                    mp_.unlink()
                except OSError:
                    pass
            stats["failed_retry"] += 1

        # pre-recognition dedup: identical content AND identical recognition
        # configuration already indexed?  The canonical target must resolve
        # directly to a valid ok result (never another duplicate).
        sha = rec.get("sha256")
        canonical = dedup_map.get((sha, current_fp)) if (sha and current_fp) else None
        if canonical == pid:
            canonical = None
        if canonical and conn is not None:
            canonical = resolve_canonical(conn, canonical)
        if canonical and canonical != pid:
            try:
                from htr_sidecar import read_sidecar
                write_duplicate_sidecar(cfg, rec, canonical)
                sc = read_sidecar(sidecar_target(cfg, rec))
                index_sidecar(cfg, sc)
                stats["duplicate"] += 1
                continue
            except Exception as e:
                log_event(cfg, "dedup_error", page_id=pid, error=repr(e))

        rec = dict(rec)
        # aliases expected by stage_a_runner's page-dict contract
        rec["image_path"] = rec["path"]
        rec["case_id"] = rec.get("case_ref") or rec.get("source_id") or "unknown"
        rec["image_sha256"] = rec.get("sha256") or ""
        rec["_new"] = (now - _epoch(rec.get("discovered_at"))) < 6 * 3600
        by_source.setdefault(rec["source_id"], []).append(rec)
    if conn is not None:
        conn.close()

    order = sorted(by_source, key=lambda s: prio.get(s, 50))
    queue: list[dict] = []
    lists = [sorted(by_source[s], key=lambda r: (not r["_new"], r["page_id"]))
             for s in order]
    while any(lists):
        for lst in lists:
            if lst:
                queue.append(lst.pop(0))
    stats["pending"] = len(queue)
    if limit:
        queue = queue[:limit]
    return queue, stats


def _epoch(ts: str | None) -> float:
    if not ts:
        return 0.0
    import datetime
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(ts, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


# ---------------------------------------------------------------- recognition

def _slice_entry(worker_id: int, pages: list[dict], do_stage_b: bool,
                 threads: int, out_root: str, hb_root: str) -> None:
    """Child process: bounded-priority recognition slice (reuses the hardened
    stage_a_runner worker: atomic checkpoints, per-page heartbeats)."""
    try:
        os.nice(int(os.environ.get("HTR_NICE", "10")))
    except OSError:
        pass
    import stage_a_runner
    stage_a_runner.worker_main(worker_id, pages, do_stage_b, threads,
                               out_root=Path(out_root),
                               hb_root=Path(hb_root))


def run_round(cfg: dict, pages: list[dict]) -> dict:
    """Spawn one recognition slice and watchdog it (single slot).

    Reuses stage_a_runner semantics: heartbeat-stale > page_timeout =>
    SIGKILL + mark in-flight page failed + respawn on remaining, capped at
    --max-restarts.  STOP (SIGTERM) terminates the child gracefully; the
    in-flight page stays pending and resumes next start.
    """
    if not pages:
        return {"processed": 0, "reason": "empty queue"}
    ocr_root, hb_dir = cfg["ocr_root"], cfg["heartbeat_dir"]
    hb_path = Path(hb_dir) / "w0.json"
    hb_path.parent.mkdir(parents=True, exist_ok=True)
    if hb_path.exists():
        hb_path.unlink()
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[var] = str(cfg["threads"])
    os.environ["HTR_NICE"] = str(cfg.get("nice", 10))
    ctx = mp.get_context("spawn")
    todo = list(pages)
    restarts = 0
    out = {"processed": 0, "killed": 0, "respawns": 0, "stopped": False}

    while todo:
        pr = ctx.Process(
            target=_slice_entry,
            args=(0, todo, bool(cfg["do_stage_b"]), int(cfg["threads"]),
                  ocr_root, hb_dir),
            name="htr-index-slice")
        pr.start()
        born = time.time()
        stopped = False
        timed_out = False
        hb: dict | None = None
        while pr.is_alive():
            if STOP.is_set():
                pr.terminate()
                pr.join(15)
                if pr.is_alive():
                    pr.kill()
                    pr.join(5)
                stopped = True
                break
            time.sleep(5)
            try:
                cand = json.loads(hb_path.read_text())
                if cand.get("pid") == pr.pid:
                    hb = cand
            except (OSError, json.JSONDecodeError):
                pass
            age = (time.time() - hb["updated_epoch"]) if hb else \
                  (time.time() - born)
            if age > int(cfg["page_timeout_s"]):
                log_event(cfg, "page_timeout", pid=pr.pid,
                          page_id=(hb or {}).get("page_id"),
                          stale_s=round(age))
                pr.kill()
                pr.join(10)
                timed_out = True
                break
        if stopped:
            out["stopped"] = True
            try:
                pr.join(5)
            except Exception:
                pass
            log_event(cfg, "worker_stopped_by_signal", pages_left=len(todo))
            return out
        pr.join(10)
        if timed_out:
            out["killed"] += 1
            page_id = (hb or {}).get("page_id")
            if page_id:
                from stage_a_runner import mark_page_failed, page_status
                if page_status(page_id, Path(ocr_root)) is None:
                    page = next((p for p in todo
                                 if p["page_id"] == page_id), None)
                    if page:
                        mark_page_failed(
                            page,
                            f"page_timeout: killed after >"
                            f"{cfg['page_timeout_s']}s without heartbeat "
                            "(retryable)", Path(ocr_root))
                        log_event(cfg, "page_marked_failed", page_id=page_id)
        elif pr.exitcode not in (0, None):
            log_event(cfg, "slice_child_exited", exitcode=pr.exitcode,
                      remaining=len(todo))

        from stage_a_runner import page_status
        todo = [p for p in todo
                if page_status(p["page_id"], Path(ocr_root)) is None]
        if not todo:
            break
        if restarts >= 3:
            log_event(cfg, "slice_deferred", reason="max_restarts",
                      pending=[p["page_id"] for p in todo])
            break
        restarts += 1
        out["respawns"] += 1
        log_event(cfg, "slice_respawned", restarts=restarts,
                  pages=len(todo))
    return out


# ---------------------------------------------------------------- finalize

def finalize(cfg: dict, pages: list[dict]) -> dict:
    """Sidecar + transactional DB index for every finished page of a round."""
    from htr_inventory import load_state, save_state
    from htr_sidecar import validate_sidecar, write_sidecar
    from stage_a_runner import page_status
    state = load_state(cfg)
    stats = {"indexed": 0, "failed": 0, "already": 0, "errors": []}
    for p in pages:
        pid = p["page_id"]
        status = page_status(pid, Path(cfg["ocr_root"]))
        if status is None:
            continue
        rec = state["files"].get(p.get("path") or "", None)
        if rec is None:
            # locate by page_id
            rec = next((r for r in state["files"].values()
                        if r.get("page_id") == pid), None)
        if rec is None:
            stats["errors"].append(f"{pid}: no inventory record")
            continue
        try:
            sc = build_sidecar_for(cfg, dict(rec, path=p["image_path"]),
                                   origin="new")
            if sc is None:
                stats["errors"].append(f"{pid}: artifacts missing")
                continue
            problems = validate_sidecar(sc)
            if problems:
                stats["errors"].append(f"{pid}: invalid sidecar {problems[:3]}")
                continue
            target = sidecar_target(cfg, dict(rec, path=p["image_path"]))
            write_sidecar(target, sc)
            index_sidecar(cfg, sc)
            if status == "ok":
                stats["indexed"] += 1
                rec.pop("attempts", None)
                rec.pop("parked", None)
                rec.pop("last_error", None)
                log_event(cfg, "page_indexed", page_id=pid,
                          lines=len(sc["lines"]),
                          elapsed_s=sc["recognition"].get("duration_s"),
                          collection=sc["source"].get("collection"))
            else:
                stats["failed"] += 1
                attempts = int(rec.get("attempts") or 0) + 1
                rec["attempts"] = attempts
                rec["last_error"] = sc["recognition"].get("error")
                if attempts >= int(cfg["max_attempts"]):
                    rec["parked"] = True
                log_event(cfg, "page_failed", page_id=pid, attempts=attempts,
                          parked=rec.get("parked", False),
                          error=sc["recognition"].get("error"))
        except Exception as e:
            stats["errors"].append(f"{pid}: {e!r}")
            log_event(cfg, "finalize_error", page_id=pid, error=repr(e))
    save_state(cfg, state)
    return stats


# ---------------------------------------------------------------- commands

def cmd_inventory(cfg: dict, ns) -> int:
    from htr_inventory import poll
    summary = poll(cfg, decode=not ns.no_decode)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


def cmd_import(cfg: dict, ns) -> int:
    from htr_import import legacy_to_sidecars
    stats = legacy_to_sidecars(cfg, force=ns.force)
    rec = reconcile(cfg)
    print(json.dumps({"legacy_import": stats, "reconcile": rec},
                     ensure_ascii=False, indent=1))
    if ns.db:
        from htr_search.db import build
        print(json.dumps(build(cfg["db_path"], import_legacy=False),
                         ensure_ascii=False, indent=1))
    return 0


def cmd_build(cfg: dict, ns) -> int:
    from htr_search.db import build
    stats = build(cfg["db_path"], import_legacy=not ns.no_import)
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    return 0


def cmd_process(cfg: dict, ns) -> int:
    from htr_inventory import poll
    poll(cfg)
    from htr_import import legacy_to_sidecars
    legacy_to_sidecars(cfg, quiet=True)
    reconcile(cfg)
    queue, qstats = build_queue(cfg, limit=ns.limit or int(cfg["round_pages"]))
    _log(f"queue: {qstats}")
    if not queue:
        _log("nothing pending")
        return 0
    _log(f"recognizing {len(queue)} page(s)…")
    round_out = run_round(cfg, queue)
    fin = finalize(cfg, queue)
    log_event(cfg, "round_finished", queue=qstats, round=round_out,
              finalize=fin)
    _log(f"round done: {fin} {round_out}")
    return 0


def cmd_daemon(cfg: dict, ns) -> int:
    acquire_lock(cfg)
    write_pid(cfg)

    def _term(signum, frame):
        STOP.set()
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    from htr_import import legacy_to_sidecars
    from htr_inventory import poll
    ensure_runtime_dirs(cfg)
    log_event(cfg, "daemon_started", pid=os.getpid(),
              config=cfg["config_path"], round_pages=cfg["round_pages"],
              threads=cfg["threads"], nice=cfg["nice"])
    _log(f"daemon started pid={os.getpid()} config={cfg['config_path']}")
    imported_once = False
    rounds = 0
    try:
        while not STOP.is_set():
            t0 = time.time()
            try:
                t = time.time()
                summary = poll(cfg)
                poll_s = round(time.time() - t, 2)
                if not imported_once:
                    st = legacy_to_sidecars(cfg, quiet=True)
                    _log(f"legacy import: {st}")
                    imported_once = True
                t = time.time()
                rec = reconcile(cfg)
                reconcile_s = round(time.time() - t, 2)
                if rec["sidecars_written"] or rec["db_upserts"]:
                    _log(f"reconcile: {rec}")
                t = time.time()
                queue, qstats = build_queue(
                    cfg, limit=int(cfg["round_pages"]))
                queue_s = round(time.time() - t, 2)
                hb = {"pid": os.getpid(), "updated_epoch": time.time(),
                      "phase": "idle" if not queue else "round",
                      "queue": qstats, "rounds": rounds,
                      "discovered": summary["discovered"]}
                Path(cfg["heartbeat_dir"], "daemon.json").write_text(
                    json.dumps(hb, ensure_ascii=False))
                if queue:
                    rounds += 1
                    _log(f"round {rounds}: {len(queue)} page(s) "
                         f"(stable={qstats['stable']} pending="
                         f"{qstats['pending']})")
                    t = time.time()
                    round_out = run_round(cfg, queue)
                    round_wall_s = round(time.time() - t, 2)
                    t = time.time()
                    fin = finalize(cfg, queue)
                    finalize_s = round(time.time() - t, 2)
                    # measured recognition time of this round's pages
                    page_sum = 0.0
                    page_n = 0
                    for p in queue:
                        try:
                            meta = json.loads(
                                (Path(cfg["ocr_root"]) / p["page_id"] /
                                 "meta.json").read_text(encoding="utf-8"))
                            page_sum += float(meta.get("elapsed_s") or 0)
                            page_n += 1
                        except (OSError, json.JSONDecodeError, ValueError):
                            pass
                    page_sum = round(page_sum, 1)
                    log_event(cfg, "round_finished", round_no=rounds,
                              queue=qstats, round=round_out, finalize=fin)
                    # P1-runtime: where wall time actually goes (theoretical
                    # 3600/avg_page vs measured rate is explained here)
                    log_event(cfg, "round_timing", round_no=rounds,
                              pages=len(queue), pages_timed=page_n,
                              poll_s=poll_s, reconcile_s=reconcile_s,
                              queue_s=queue_s, round_wall_s=round_wall_s,
                              finalize_s=finalize_s,
                              page_elapsed_sum_s=page_sum,
                              round_overhead_s=round(round_wall_s - page_sum,
                                                     2),
                              loop_s=round(time.time() - t0, 2))
                    _log(f"round {rounds} done: {fin} "
                         f"wall={round_wall_s}s pages={page_sum}s")
                    if round_out.get("stopped"):
                        break
                    if fin["indexed"] == 0 and fin["failed"] == 0:
                        slp = int(cfg["poll_interval_s"])
                        log_event(cfg, "worker_sleep", seconds=slp)
                        time.sleep(slp)
                else:
                    slp = int(cfg["poll_interval_s"])
                    log_event(cfg, "worker_sleep", seconds=slp)
                    time.sleep(slp)
            except Exception as e:
                log_event(cfg, "daemon_error", error=repr(e),
                          traceback=traceback.format_exc()[-2000:])
                _log(f"ERROR: {e!r}")
                time.sleep(int(cfg["poll_interval_s"]))
            finally:
                _ = t0
    finally:
        log_event(cfg, "daemon_stopped", rounds=rounds)
        Path(cfg["pid_path"]).unlink(missing_ok=True)
        release_lock()
        _log("daemon stopped cleanly")
    return 0


def cmd_stop(cfg: dict, ns) -> int:
    pid = read_pid(cfg)
    if pid is None:
        print("no worker pid file — nothing to stop")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"pid {pid} not running; cleaning up")
        Path(cfg["pid_path"]).unlink(missing_ok=True)
        return 1
    for _ in range(60):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"worker {pid} stopped")
            return 0
    print(f"worker {pid} still alive after 30s — check manually")
    return 2


def cmd_status(cfg: dict, ns) -> int:
    from htr_monitor import report
    report(cfg)
    return 0


def cmd_retry(cfg: dict, ns) -> int:
    from htr_inventory import load_state, save_state
    from stage_a_runner import meta_path
    state = load_state(cfg)
    n = 0
    for rec in state["files"].values():
        if rec.get("attempts") or rec.get("parked"):
            if ns.all or int(rec.get("attempts") or 0) >= int(cfg["max_attempts"]):
                rec.pop("attempts", None)
                rec.pop("parked", None)
                n += 1
            if ns.clear_markers:
                for root in (cfg["ocr_root"], cfg["legacy_ocr_root"]):
                    mp_ = meta_path(rec["page_id"], Path(root))
                    if mp_.exists() and ns.clear_markers:
                        try:
                            mp_.unlink()
                        except OSError:
                            pass
    save_state(cfg, state)
    print(f"reset {n} page(s) for retry")
    return 0


def cmd_overlay(cfg: dict, ns) -> int:
    """Draw sidecar bboxes over the original image (alignment verification)."""
    from PIL import Image, ImageDraw
    from htr_sidecar import read_sidecar
    conn = db_conn(cfg, readonly=True)
    row = conn.execute("SELECT image_path FROM pages WHERE page_id=?",
                       (ns.page_id,)).fetchone()
    conn.close()
    if row is None:
        print(f"no page {ns.page_id}")
        return 1
    # find the sidecar via inventory
    from htr_inventory import stable_files
    rec = next((r for r in stable_files(cfg)
                if r["page_id"] == ns.page_id), None)
    if rec is None:
        print("no inventory record")
        return 1
    sc = read_sidecar(sidecar_target(cfg, rec))
    img = Image.open(row["image_path"]).convert("RGB")
    dr = ImageDraw.Draw(img)
    for ln in sc["lines"]:
        x0, y0, x1, y1 = ln["bbox"]
        color = (255, 60, 60) if ln["source"] == "blla-ocr" else (60, 160, 255)
        dr.rectangle([x0, y0, x1, y1], outline=color, width=3)
    out = Path(cfg["logs_dir"]).parent / "overlays" / f"{ns.page_id}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=85)
    print(out)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="path to htr-index.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inventory", help="one discovery pass")
    p.add_argument("--no-decode", action="store_true")

    p = sub.add_parser("import", help="legacy artifacts -> sidecars -> DB")
    p.add_argument("--force", action="store_true")
    p.add_argument("--db", action="store_true", help="also (re)build the DB")

    p = sub.add_parser("build", help="rebuild DB from sidecars")
    p.add_argument("--no-import", action="store_true",
                   help="skip legacy import step")

    p = sub.add_parser("process", help="one processing round")
    p.add_argument("--limit", type=int, default=0,
                   help="max pages this round (default: round_pages)")

    sub.add_parser("daemon", help="continuous worker (lock-guarded)")
    sub.add_parser("status", help="read-only progress report")
    sub.add_parser("stop", help="graceful shutdown of the daemon")

    p = sub.add_parser("retry", help="reset failed/parked pages")
    p.add_argument("--all", action="store_true",
                   help="reset every page with attempts, not only parked")
    p.add_argument("--clear-markers", action="store_true",
                   help="also delete failed meta.json markers")

    p = sub.add_parser("overlay", help="draw sidecar bboxes on the original")
    p.add_argument("page_id")

    ns = ap.parse_args()
    cfg = load_config(ns.config)
    if ns.cmd == "inventory":
        return cmd_inventory(cfg, ns)
    if ns.cmd == "import":
        return cmd_import(cfg, ns)
    if ns.cmd == "build":
        return cmd_build(cfg, ns)
    if ns.cmd == "process":
        return cmd_process(cfg, ns)
    if ns.cmd == "daemon":
        return cmd_daemon(cfg, ns)
    if ns.cmd == "status":
        return cmd_status(cfg, ns)
    if ns.cmd == "stop":
        return cmd_stop(cfg, ns)
    if ns.cmd == "retry":
        return cmd_retry(cfg, ns)
    if ns.cmd == "overlay":
        return cmd_overlay(cfg, ns)
    return 2


if __name__ == "__main__":
    sys.exit(main())
