#!/usr/bin/env python
"""KINSHIP-HTR-BENCH-01: compare two engine runs sharing the same PAGE XML lines.

Protocol for additional engines: run polyscriptor batch_processing.py with
  --use-pagexml --xml-folder <page_xml> --segmentation-method none
so every engine consumes the SAME line crops (blla coordinates exported once).
Then compare per-line outputs:

Usage: compare.py <runA.csv-dir> <runB.csv-dir> <out.md>
  <csv-dir> = folder with per-image transcriptions/*.csv (line_number,text,confidence)
"""
import csv
import json
import sys
from pathlib import Path


def load(run_dir: Path) -> dict:
    out = {}
    for f in sorted((run_dir / 'transcriptions').glob('*.csv')):
        page = f.stem
        rows = list(csv.DictReader(open(f, encoding='utf-8')))
        out[page] = {int(r['line_number']): r['text'] for r in rows}
    return out


def main(a_dir: Path, b_dir: Path, out_md: Path) -> None:
    A, B = load(a_dir), load(b_dir)
    agree = disagree = missing = 0
    rows = []
    for page in sorted(set(A) | set(B)):
        for ln in sorted(set(A.get(page, {})) | set(B.get(page, {}))):
            ta = A.get(page, {}).get(ln)
            tb = B.get(page, {}).get(ln)
            if ta is None or tb is None:
                missing += 1
                continue
            if ta.strip() == tb.strip():
                agree += 1
            else:
                disagree += 1
                rows.append((page, ln, ta, tb))
    total = agree + disagree
    md = ["# Engine comparison (same PAGE-XML line set)\n",
          f"- engine A: `{a_dir}`", f"- engine B: `{b_dir}`",
          f"- identical lines: {agree}/{total} ({agree / total:.1%})" if total else "- no common lines",
          f"- lines missing on one side: {missing}\n",
          "| page | line | A | B |", "|---|---|---|---|"]
    for page, ln, ta, tb in rows[:200]:
        md.append(f"| {page} | {ln} | {ta[:50]} | {tb[:50]} |")
    out_md.write_text('\n'.join(md), encoding='utf-8')
    print('\n'.join(md[:6]))


if __name__ == '__main__':
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
