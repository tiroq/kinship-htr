#!/bin/bash
# KINSHIP-HTR-BENCH-01 — reproducible HTR benchmark run
# Stack: polyscriptor (git 52524ed) batch CLI, engine crnn-ctc,
#        model achimrabus/crnn-ctc-russian (Preset "Russian (6.92% CER)"),
#        segmentation kraken-blla (pagexml/blla.mlmodel shipped with polyscriptor).
# Environment: ARM64 miniconda env "htrbench" (python 3.12, torch 2.14.0, kraken 7.1.1)
# See env-versions.txt for full pinned versions and model sha256 hashes.
#
# Usage: run_bench.sh <input-folder> <output-folder>
set -euo pipefail

ENV_PY=/Users/mysterx/miniconda3/envs/htrbench/bin/python
POLY=/Users/mysterx/dev/kinship-htr-bench/polyscriptor
IN=${1:?input folder with page scans}
OUT=${2:?output folder}

"$ENV_PY" "$POLY/batch_processing.py" \
  --input-folder "$IN" \
  --engine crnn-ctc \
  --model-path "Russian (6.92% CER)" \
  --segmentation-method kraken-blla \
  --device cpu \
  --output-folder "$OUT" \
  --output-format txt \
  --output-format csv \
  --output-format pagexml \
  --output-format json \
  --verbose
