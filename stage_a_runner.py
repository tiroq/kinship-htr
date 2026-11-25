#!/usr/bin/env python3
"""KINSHIP-HTR-SEARCH-01 Stage A+B: resumable local OCR of all 329 CGAMOS pages.

Reuses the proven 8-page benchmark artifacts when provenance matches, then
processes remaining pages with the pinned htrbench environment:
  engine  crnn-ctc (achimrabus/crnn-ctc-russian, sha256 74482bd0…)
  seg     kraken-blla (pagexml/blla.mlmodel, sha256 77a638a8…)
  patch   bench-patch.diff (min line 15px + width guard) — already applied
Outputs per page:  work/index/ocr/<page_id>/
  transcriptions/<page_id>.{json,txt,csv}   page_xml/<page_id>.xml
  meta.json (status+provenance)             stage_b.json (gap recovery)

Resume: re-run the script; pages whose meta.json has status ok/failed are
skipped.  At startup every ok page is audited (all four artifacts present,
non-empty, decodable); broken markers are demoted and recomputed.  Valid
completed pages are never recomputed.

Hardening (2026-11-21 stall, see REPORT.md §10):
  * meta.json / stage_b.json are written atomically (tmp + os.replace);
    meta.json is the completion marker and is written last, so a kill at any
    point leaves a page either fully valid or fully absent.
  * workers heartbeat per page to work/index/heartbeat/wK.json; the parent
    watchdog (replacing the old unconditional join()) SIGKILLs a worker
    whose heartbeat is stale beyond --page-timeout, marks the in-flight
    page failed (re-runnable via --redo) and respawns the worker on its
    remaining pages, up to --max-restarts per slot.
  * worker stdout uses safe_print(): a lost/broken console (observed
    17:06 log cutoff) can no longer raise inside the page loop and mark
    good pages failed.
  * native thread pools (OMP/MKL/OpenBLAS/Accelerate) are bounded in the
    parent before spawn; torch intra+interop threads are bounded per child.
  * progress heartbeat events append to work/index/logs/progress.jsonl.

Usage:  htrbench-python stage_a_runner.py [--workers 1] [--cases 253,255]
        [--limit N] [--no-stage-b] [--redo FAILED_PAGES]
        [--page-timeout 900] [--poll-interval 10] [--max-restarts 3]
        [--threads 1]
Run from /Users/mysterx/dev/kinship-htr-bench with the htrbench env python.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

BENCH = Path("/Users/mysterx/dev/kinship-htr-bench")
POLY = BENCH / "polyscriptor"
OUT_ROOT = BENCH / "work" / "index" / "ocr"
MANIFEST = BENCH / "work" / "index_manifest.json"
BENCH8 = BENCH / "work" / "out-8pages"
LOGDIR = BENCH / "work" / "index" / "logs"
HB_ROOT = BENCH / "work" / "index" / "heartbeat"
PROGRESS_LOG = LOGDIR / "progress.jsonl"

MODEL_SHA = "74482bd0eb8e33b25a197d0452559b888809dd0ae4e014274301a513f553dd32"
BLLA_SHA = "77a638a83c9e535620827a09e410ed36391e9e8e8366d5796a0f15b978186056"
PATCH_ID = "bench-patch.diff (min_height 40->15 + width guard, applied)"

CASES = {"253": "0597-0001-000253", "255": "0597-0001-000255",
         "256": "0597-0001-000256", "258": "0597-0001-000258"}
BENCH8_PAGES = {"253-p003", "253-p032", "255-p003", "255-p027",
                "256-p003", "256-p050", "258-p003", "258-p049"}

MIN_FREE_GB = 2.0

PAGE_ARTIFACTS = ("transcriptions/{pid}.json", "transcriptions/{pid}.txt",
                  "transcriptions/{pid}.csv", "page_xml/{pid}.xml")


def disk_ok() -> bool:
    st = os.statvfs(BENCH)
    free = st.f_bavail * st.f_frsize / 2**30
    if free < MIN_FREE_GB:
        print(f"ABORT: only {free:.1f} GB free (budget {MIN_FREE_GB} GB)", file=sys.stderr)
        return False
    print(f"disk preflight: {free:.1f} GB free (budget {MIN_FREE_GB} GB minimum)")
    return True


def atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def log_event(event: str, **kw) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event}
    rec.update(kw)
    with PROGRESS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


_stdout_dead = False


def safe_print(*args) -> None:
    """print() that survives a lost/broken console.

    During the 2026-11-21 stall the console redirect died mid-run; a plain
    print() raising BrokenPipeError/OSError inside the page loop would have
    marked a finished page as failed.  After one failure stdout is swapped
    to /dev/null and the worker keeps writing its real checkpoints.
    """
    global _stdout_dead
    if _stdout_dead:
        return
    try:
        print(*args, flush=True)
    except OSError:
        _stdout_dead = True
        try:
            sys.stdout = open(os.devnull, "w")
        except Exception:
            pass


def load_pages() -> list[dict]:
    raw = json.loads(MANIFEST.read_text())
    pages = []
    for pid, (path, sha, w, h) in sorted(raw.items()):
        short = pid.split("-")[0]
        pages.append({"page_id": pid, "case_short": short, "case_id": CASES[short],
                      "page_number": int(pid.split("p")[-1]), "image_path": path,
                      "image_sha256": sha, "width": w, "height": h})
    return pages


def provenance(page: dict, reused: bool = False) -> dict:
    return {
        "page_id": page["page_id"], "case_id": page["case_id"],
        "page_number": page["page_number"], "image_path": page["image_path"],
        "image_sha256": page["image_sha256"],
        "engine": "crnn-ctc (PyLaia-inspired CRNN-CTC)",
        "model": "achimrabus/crnn-ctc-russian 'Russian (6.92% CER)'",
        "model_sha256": MODEL_SHA, "segmentation": "kraken-blla",
        "blla_sha256": BLLA_SHA, "patch": PATCH_ID,
        "env": "miniconda htrbench (see env-versions.txt; python 3.12, torch 2.14.0, kraken 7.1.1)",
        "device": "cpu", "reused_from_benchmark": reused,
    }


def meta_path(page_id: str) -> Path:
    return OUT_ROOT / page_id / "meta.json"


def page_status(page_id: str) -> str | None:
    mp_ = meta_path(page_id)
    if not mp_.exists():
        return None
    try:
        return json.loads(mp_.read_text())["status"]
    except Exception:
        return None


def checkpoint_problems(page_id: str) -> list[str]:
    """Missing/empty/undecodable artifacts for a page marked ok."""
    d = OUT_ROOT / page_id
    probs = []
    for rel_t in PAGE_ARTIFACTS:
        rel = rel_t.format(pid=page_id)
        f = d / rel
        if not f.exists():
            probs.append(rel + " missing")
            continue
        if f.stat().st_size == 0:
            probs.append(rel + " empty")
            continue
        if rel.endswith(".json"):
            try:
                json.loads(f.read_text())
            except Exception:
                probs.append(rel + " corrupt json")
        elif rel.endswith(".xml"):
            import xml.etree.ElementTree as ET
            try:
                ET.parse(f)
            except Exception:
                probs.append(rel + " corrupt xml")
    return probs


def audit_ok(pages: list[dict]) -> list[str]:
    """Verify every ok page has complete decodable artifacts; demote broken
    checkpoint markers so those pages recompute.  Valid pages are kept."""
    demoted = []
    for p in pages:
        pid = p["page_id"]
        if page_status(pid) != "ok":
            continue
        probs = checkpoint_problems(pid)
        if probs:
            meta_path(pid).unlink()
            log_event("checkpoint_demoted", page_id=pid, problems=probs)
            safe_print(f"AUDIT: demoted broken checkpoint {pid}: {probs}")
            demoted.append(pid)
    return demoted


def reuse_bench8(pages: list[dict]) -> int:
    by_id = {p["page_id"]: p for p in pages}
    reused = 0
    for pid in sorted(BENCH8_PAGES):
        p = by_id.get(pid)
        if p is None or page_status(pid) is not None:
            continue
        d = OUT_ROOT / pid
        (d / "transcriptions").mkdir(parents=True, exist_ok=True)
        (d / "page_xml").mkdir(parents=True, exist_ok=True)
        ok = True
        for rel_t in PAGE_ARTIFACTS:
            src = BENCH8 / rel_t.format(pid=pid)
            if src.exists():
                shutil.copy2(src, d / rel_t.format(pid=pid))
            else:
                ok = False
        meta = provenance(p, reused=True)
        meta["status"] = "ok" if ok else "failed"
        meta["source_dir"] = str(BENCH8)
        meta["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        atomic_write_json(d / "meta.json", meta)
        if ok:
            reused += 1
            print(f"reused bench8 page {pid}")
    return reused


def mark_page_failed(page: dict, error: str) -> None:
    mp = meta_path(page["page_id"])
    mp.parent.mkdir(parents=True, exist_ok=True)  # failure can precede any mkdir
    meta = provenance(page)
    meta["status"] = "failed"
    meta["error"] = error
    meta["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    atomic_write_json(mp, meta)


def worker_main(worker_id: int, pages: list[dict], do_stage_b: bool, threads: int):
    """Runs in a spawned child: holds models in memory, processes its page slice.

    Durability: meta.json is written last and atomically; a kill at any point
    leaves the page either fully valid or fully absent.  Heartbeats go to
    HB_ROOT/w<worker_id>.json so the parent watchdog can time out a stuck
    page and kill this process externally.
    """
    hb_path = HB_ROOT / f"w{worker_id}.json"

    def heartbeat(page_id, phase, **kw):
        rec = {"pid": os.getpid(), "worker": worker_id, "phase": phase,
               "page_id": page_id, "updated_epoch": time.time()}
        rec.update(kw)
        atomic_write_json(hb_path, rec)

    HB_ROOT.mkdir(parents=True, exist_ok=True)
    heartbeat(None, "init")
    sys.path.insert(0, str(POLY))
    import numpy as np  # noqa: F401
    import torch
    torch.set_num_threads(threads)
    try:
        torch.set_num_interop_threads(threads)
    except RuntimeError:
        pass  # interop pool already initialized

    import stage_b_layout
    from batch_processing import BatchHTRProcessor, parse_args
    import logging
    tmp = OUT_ROOT / f"_tmp_worker{worker_id}"
    (tmp / "input").mkdir(parents=True, exist_ok=True)
    sys.argv = ["batch_processing.py",
                "--input-folder", str(tmp / "input"),
                "--engine", "crnn-ctc",
                "--model-path", "Russian (6.92% CER)",
                "--segmentation-method", "kraken-blla",
                "--device", "cpu",
                "--output-folder", str(tmp),
                "--output-format", "txt",
                "--output-format", "csv",
                "--output-format", "pagexml",
                "--output-format", "json"]
    args = parse_args()
    proc = BatchHTRProcessor(args)
    proc.initialize()
    # quiet console handler; per-worker file log keeps DEBUG
    for h in logging.getLogger('batch_htr').handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(logging.ERROR)

    for i, p in enumerate(pages):
        pid = p["page_id"]
        if page_status(pid) is not None:  # another worker / earlier run finished it
            continue
        heartbeat(pid, "page", index=i + 1, total=len(pages))
        t0 = time.time()
        d = OUT_ROOT / pid
        try:
            img = tmp / "input" / f"{pid}.jpg"
            if img.is_symlink() or img.exists():
                img.unlink()
            img.symlink_to(p["image_path"])
            # clear any half-written leftovers from a killed earlier attempt
            for rel_t in PAGE_ARTIFACTS:
                f = tmp / rel_t.format(pid=pid)
                if f.exists():
                    f.unlink()

            result = proc.process_image(img)
            if result.get("status") == "skipped (no lines)":
                raise RuntimeError("segmentation produced no lines")

            (d / "transcriptions").mkdir(parents=True, exist_ok=True)
            (d / "page_xml").mkdir(parents=True, exist_ok=True)
            for rel_t in PAGE_ARTIFACTS:
                src = tmp / rel_t.format(pid=pid)
                if src.exists():
                    os.replace(str(src), str(d / rel_t.format(pid=pid)))

            stage_b = {"provenance": "stage-b-gap-recovery (v1 projection-profile, "
                        "Otsu binarization, uncovered ink bands)", "lines": [],
                       "bands_examined": 0}
            if do_stage_b:
                from PIL import Image
                image = Image.open(p["image_path"]).convert("L")
                existing = []
                tj = d / f"transcriptions/{pid}.json"
                if tj.exists():
                    for ln in json.loads(tj.read_text())["lines"]:
                        existing.append(tuple(ln["bbox"]))
                stage_b = stage_b_layout.recover_uncovered(
                    proc.engine, image, existing)

            meta = provenance(p)
            meta["status"] = "ok"
            meta["line_count"] = result.get("line_count")
            meta["extra_line_count"] = len(stage_b["lines"])
            meta["bands_examined"] = stage_b["bands_examined"]
            meta["avg_confidence"] = result.get("avg_confidence")
            meta["elapsed_s"] = round(time.time() - t0, 1)
            meta["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            atomic_write_json(d / "stage_b.json", stage_b)
            atomic_write_json(d / "meta.json", meta)  # completion marker, last
            heartbeat(pid, "ok", elapsed_s=meta["elapsed_s"])
            safe_print(f"[w{worker_id}] OK {pid} lines={meta['line_count']} "
                       f"extra={meta['extra_line_count']} {meta['elapsed_s']}s "
                       f"({i + 1}/{len(pages)})")
        except Exception as e:
            d.mkdir(parents=True, exist_ok=True)  # failure can precede any mkdir
            meta = provenance(p)
            meta["status"] = "failed"
            meta["error"] = f"{type(e).__name__}: {e}"
            meta["traceback_tail"] = traceback.format_exc()[-1500:]
            meta["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            atomic_write_json(d / "meta.json", meta)
            safe_print(f"[w{worker_id}] FAIL {pid}: {e}")

    try:
        proc.cleanup()
    except Exception:
        pass
    heartbeat(None, "exit")
    safe_print(f"[w{worker_id}] slice done")


def remaining(pages: list[dict]) -> list[dict]:
    return [p for p in pages if page_status(p["page_id"]) is None]


def supervise(nw: int, slices: list[list[dict]], do_stage_b: bool,
              pages: list[dict], ns) -> None:
    """Watchdog replacing the old unconditional pr.join().

    Old behavior blocked forever when workers hung (observed 2026-11-21
    ~17:25: all four workers silent, no exceptions, join() never returned).
    Now: a worker whose heartbeat is stale beyond --page-timeout is SIGKILLed
    (external kill of a stuck child), its in-flight page is marked failed
    (re-runnable via --redo) and it respawns on its remaining pages, up to
    --max-restarts per slot.  Progress heartbeats append to progress.jsonl.
    """
    HB_ROOT.mkdir(parents=True, exist_ok=True)
    for f in HB_ROOT.glob("w*.json"):
        f.unlink()  # scratch state from an earlier run, not checkpoints

    slots = {}
    for k in range(nw):
        pr = mp.Process(target=worker_main,
                        args=(k, slices[k], do_stage_b, ns.threads),
                        name=f"htr-w{k}")
        pr.start()
        slots[k] = {"pr": pr, "todo": slices[k], "restarts": 0,
                    "born": time.time()}
        log_event("worker_started", worker=k, pid=pr.pid, pages=len(slices[k]))

    last_beat = 0.0
    while slots:
        time.sleep(ns.poll_interval)
        now = time.time()
        for k in sorted(slots):
            s = slots[k]
            pr = s["pr"]
            try:
                hb = json.loads((HB_ROOT / f"w{k}.json").read_text())
            except Exception:
                hb = None
            ours = hb is not None and hb.get("pid") == pr.pid
            if pr.is_alive():
                hb_age = (now - hb["updated_epoch"]) if ours else (now - s["born"])
                if hb_age <= ns.page_timeout:
                    continue  # healthy
                log_event("page_timeout", worker=k, pid=pr.pid,
                          page_id=hb.get("page_id") if ours else None,
                          stale_s=round(hb_age))
                pr.kill()
                pr.join(10)
            else:
                pr.join(10)
                log_event("worker_exited", worker=k, pid=pr.pid,
                          exitcode=pr.exitcode)
            if ours and hb.get("page_id") and page_status(hb["page_id"]) is None:
                try:
                    page = next(p for p in s["todo"]
                                if p["page_id"] == hb["page_id"])
                    mark_page_failed(page, "page_timeout: worker killed by "
                                     f"watchdog after >{ns.page_timeout}s "
                                     "without heartbeat progress "
                                     "(re-runnable with --redo)")
                    log_event("page_marked_failed", worker=k,
                              page_id=hb["page_id"])
                except Exception as e:  # never let one slot kill the run
                    log_event("watchdog_error", worker=k,
                              where="mark_page_failed", error=repr(e))
            todo = remaining(s["todo"])
            if not todo:
                log_event("slice_done", worker=k, restarts=s["restarts"])
                del slots[k]
                continue
            if s["restarts"] >= ns.max_restarts:
                log_event("worker_disabled", worker=k, reason="max_restarts",
                          pending=[p["page_id"] for p in todo])
                del slots[k]
                continue
            s["restarts"] += 1
            pr = mp.Process(target=worker_main,
                            args=(k, todo, do_stage_b, ns.threads),
                            name=f"htr-w{k}")
            pr.start()
            s.update(pr=pr, todo=todo, born=time.time())
            log_event("worker_respawned", worker=k, pid=pr.pid,
                      restarts=s["restarts"], pages=len(todo))
        try:  # never let one slot's handling kill the whole run
            if slots and now - last_beat >= 60:
                last_beat = now
                beat = {}
                for k in sorted(slots):
                    s = slots[k]
                    try:
                        hb = json.loads((HB_ROOT / f"w{k}.json").read_text())
                    except Exception:
                        hb = None
                    if hb and hb.get("pid") == s["pr"].pid:
                        beat[f"w{k}"] = {"phase": hb.get("phase"),
                                         "page_id": hb.get("page_id"),
                                         "age_s": round(now - hb["updated_epoch"])}
                    else:
                        beat[f"w{k}"] = {"phase": "starting",
                                         "age_s": round(now - s["born"])}
                ok = sum(1 for p in pages if page_status(p["page_id"]) == "ok")
                log_event("heartbeat", ok=ok, pending=len(pages) - ok, workers=beat)
        except Exception as e:
            log_event("watchdog_error", worker="heartbeat",
                      where="heartbeat", error=repr(e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--cases", type=str, default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-stage-b", action="store_true")
    ap.add_argument("--redo", type=str, default="",
                    help="comma-separated page_ids to re-run (deletes markers first)")
    ap.add_argument("--page-timeout", type=int, default=900,
                    help="kill a worker whose page heartbeat is older than this")
    ap.add_argument("--poll-interval", type=int, default=10)
    ap.add_argument("--max-restarts", type=int, default=3)
    ap.add_argument("--threads", type=int, default=1,
                    help="bound for OMP/MKL/torch intra+interop threads")
    ns = ap.parse_args()

    # bound native thread pools in the parent BEFORE spawn, so children
    # inherit the values at library-load time (no oversubscription)
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[var] = str(ns.threads)

    if not disk_ok():
        sys.exit(2)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    LOGDIR.mkdir(parents=True, exist_ok=True)
    log_event("run_started", workers=ns.workers, threads=ns.threads,
              page_timeout=ns.page_timeout, argv=sys.argv)

    pages = load_pages()
    if ns.cases:
        keep = set(ns.cases.split(","))
        pages = [p for p in pages if p["case_short"] in keep]
    if ns.redo:
        for pid in ns.redo.split(","):
            pid = pid.strip()
            if pid and meta_path(pid).exists():
                meta_path(pid).unlink()
                print(f"--redo: cleared marker {pid}")

    reused = reuse_bench8(pages)
    demoted = audit_ok(pages)
    pending = [p for p in pages if page_status(p["page_id"]) is None]
    if ns.limit:
        pending = pending[:ns.limit]
    print(f"pages total={len(pages)} reused_bench8={reused} "
          f"demoted={len(demoted)} pending={len(pending)}")
    if demoted:
        log_event("audit_demoted", pages=demoted)

    if not pending:
        done = sum(1 for p in pages if page_status(p["page_id"]) == "ok")
        failed = sum(1 for p in pages if page_status(p["page_id"]) == "failed")
        print(f"nothing pending; ok={done} failed={failed}")
        log_event("run_finished", ok=done, failed=failed)
        return

    sys.path.insert(0, str(POLY))
    mp.set_start_method("spawn", force=True)
    nw = max(1, min(ns.workers, len(pending)))
    slices = [pending[k::nw] for k in range(nw)]
    supervise(nw, slices, not ns.no_stage_b, pages, ns)

    ok = sum(1 for p in pages if page_status(p["page_id"]) == "ok")
    failed = [p["page_id"] for p in pages if page_status(p["page_id"]) == "failed"]
    print(f"run finished: ok={ok}/{len(pages)} failed={len(failed)} {failed}")
    log_event("run_finished", ok=ok, failed=failed, failed_pages=failed)


if __name__ == "__main__":
    main()
