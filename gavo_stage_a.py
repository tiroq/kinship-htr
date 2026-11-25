#!/usr/bin/env python3
"""KINSHIP-SHAMRAI-04 Stage A: local HTR of ГАВО ф.И-331 оп.3 д.611 (1907, Михайловка).

Reuses the proven, hardened worker from stage_a_runner.py verbatim
(models, checkpointing, atomic meta.json, heartbeats all inherited).
Pages come from the acquisition manifest of the GAVO case instead of CGAMOS.

Run from /Users/mysterx/dev/kinship-htr-bench with the htrbench env python:
  OMP_NUM_THREADS=1 /Users/mysterx/miniconda3/envs/htrbench/bin/python gavo_stage_a.py [--redo PID,...]

Outputs: work/index/ocr/g611-pNNN/ (same artifact layout as CGAMOS pages).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from pathlib import Path

BENCH = Path("/Users/mysterx/dev/kinship-htr-bench")
GAVO = Path("/Users/mysterx/Downloads/kinship-gavo/I-331-op3-d611")
MANIFEST = GAVO / "manifest.csv"
WORKLIST = BENCH / "work" / "gavo_stage_a_pages.json"


def load_pages() -> list[dict]:
    pages = []
    with open(MANIFEST, newline="") as f:
        for row in csv.DictReader(f):
            pid = f"g611-p{int(row['seq']):03d}"
            pages.append({
                "page_id": pid,
                "case_short": "g611",
                "case_id": "gavo-I331-3-611",
                "page_number": int(row["seq"]),
                "image_path": str(GAVO / "originals" / row["file"]),
                "image_sha256": row["sha256"],
                "width": int(row["width"]),
                "height": int(row["height"]),
            })
    return pages

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--redo", default="", help="comma-separated page ids to recompute")
    ap.add_argument("--limit", type=int, default=0, help="process only first N pending pages")
    args, _unknown = ap.parse_known_args()

    pages = load_pages()
    if args.redo:
        redo = set(args.redo.split(","))
        import stage_a_runner as sar
        for pid in redo:
            p = BENCH / "work" / "index" / "ocr" / pid / "meta.json"
            if p.exists():
                p.unlink()
    WORKLIST.write_text(json.dumps(pages, ensure_ascii=False, indent=1))

    import stage_a_runner as sar

    todo = [p for p in pages if sar.page_status(p["page_id"]) is None]
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print(f"limited to {len(todo)} pages for this run", flush=True)
        return

    t0 = time.time()
    # worker_main is spawn-safe and runs fine in-process; it holds the models
    # for the whole slice and checkpoints every page atomically.
    sar.worker_main(0, todo, do_stage_b=True, threads=1)
    print(f"done in {round(time.time() - t0)}s; ok={sum(1 for p in pages if sar.page_status(p['page_id']) == 'ok')}/{len(pages)}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
