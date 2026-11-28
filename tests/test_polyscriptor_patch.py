#!/usr/bin/env python3
"""Guard: the Polyscriptor working-tree patch stays applied and consolidated.

Historical note: the patch formerly existed twice as `bench-patch.diff` and
`polyscriptor-local.patch` with identical content; it was consolidated into
`polyscriptor-local.patch` (README §History).  This test pins both facts:

  1. the polyscriptor checkout's working-tree diff EQUALS
     polyscriptor-local.patch (the known line-height 40->15 + width guard
     patch — if it drifts or is applied twice, this fails);
  2. `bench-patch.diff` stays deleted (no second divergent copy).

Run:  htrbench-python tests/test_polyscriptor_patch.py   (pytest-compatible)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAIL: list[str] = []


def check(cond, msg):
    if cond:
        print(f"PASS {msg}")
    else:
        FAIL.append(msg)
        print(f"FAIL {msg}")


def main() -> int:
    if not (REPO / "polyscriptor" / ".git").exists():
        # local integration guard: the checkout (and its working-tree patch)
        # is not part of the repository; CI runs the self-contained suites
        print("SKIP test_polyscriptor_patch: local polyscriptor checkout "
              "required (integration-only)")
        return 0
    patch_path = REPO / "polyscriptor-local.patch"
    check(patch_path.exists(), "polyscriptor-local.patch present")
    if not patch_path.exists():
        return 1

    r = subprocess.run(["git", "-C", str(REPO / "polyscriptor"), "diff"],
                       capture_output=True, text=True, timeout=30)
    check(r.returncode == 0, "polyscriptor is a git checkout")
    applied = r.stdout
    recorded = patch_path.read_text()
    check(applied == recorded,
          "working-tree patch == polyscriptor-local.patch "
          "(applied exactly once, no drift)")
    check("min_height_for_cnn = 15" in applied or
          "min_height_for_cnn" in applied and "15" in applied,
          "line-height reduction present in applied patch")
    check(not (REPO / "bench-patch.diff").exists(),
          "bench-patch.diff stays consolidated (no divergent second copy)")

    # no double application: a second application would fail/duplicate hunks
    r2 = subprocess.run(
        ["git", "-C", str(REPO / "polyscriptor"), "apply", "--check",
         str(patch_path)], capture_output=True, text=True, timeout=30)
    check(r2.returncode != 0,
          "patch cannot be applied again (already applied exactly once)")

    if FAIL:
        print(f"FAIL ({len(FAIL)}):")
        for f in FAIL:
            print("  -", f)
        return 1
    print("test_polyscriptor_patch: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
