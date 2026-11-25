#!/usr/bin/env python3
"""KINSHIP-SHAMRAI-04: export evidence crops for candidate HTR lines (ГАВО д.611).

For a list of (page_id, line_number[, context_lines]) hits, crops the bbox from
the ORIGINAL scan (untouched) with generous padding, plus a wider context crop
covering neighbouring lines, into the acquisition staging directory:

  ~/Downloads/kinship-gavo/I-331-op3-d611/candidates/<page>_l<NNN>.jpg
  ~/Downloads/kinship-gavo/I-331-op3-d611/candidates/<page>_l<NNN>_ctx.jpg

Usage: gavo_candidate_crops.py "g611-p042:153" ["g611-p042:153+3" ...]
       (suffix +N = include N lines of context either side)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image

BENCH = Path("/Users/mysterx/dev/kinship-htr-bench")
OCR = BENCH / "work" / "index" / "ocr"
GAVO = Path("/Users/mysterx/Downloads/kinship-gavo/I-331-op3-d611")
OUT = GAVO / "candidates"

PAD = 40


def line_data(page_id: str, line_no: int) -> tuple[Path, list[dict]]:
    tj = OCR / page_id / "transcriptions" / f"{page_id}.json"
    page = json.loads(tj.read_text())
    img_path = Path(page["image"].replace("input", str(GAVO / "originals")))
    # the worker symlinked originals as input/<pid>.jpg; resolve from manifest
    if not img_path.exists():
        img_path = GAVO / "originals" / f"И331Оп3Д611_{int(page_id.split('p')[-1]):04d}.jpg"
    lines = sorted(page["lines"], key=lambda l: (l["bbox"][1], l["bbox"][0]))
    return img_path, lines

def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for spec in sys.argv[1:]:
        ctx = 0
        if "+" in spec:
            spec, ctx = spec.split("+")
            ctx = int(ctx)
        page_id, line_no = spec.split(":")
        line_no = int(line_no)
        img_path, lines = line_data(page_id, line_no)
        img = Image.open(img_path)
        img.load()
        idx = next(i for i, l in enumerate(lines) if l["line_number"] == line_no)
        x, y, w, h = lines[idx]["bbox"]
        crop = img.crop((max(0, x - PAD), max(0, y - PAD),
                         min(img.width, x + w + PAD), min(img.height, y + h + PAD)))
        out1 = OUT / f"{page_id}_l{line_no:03d}.jpg"
        crop.save(out1, quality=92)
        print(out1.name, crop.size)
        if ctx:
            lo = max(0, idx - ctx)
            hi = min(len(lines), idx + ctx + 1)
            band = lines[lo:hi]
            x0 = max(0, min(l["bbox"][0] for l in band) - PAD)
            y0 = max(0, band[0]["bbox"][1] - PAD)
            y1 = min(img.height, band[-1]["bbox"][1] + band[-1]["bbox"][3] + PAD)
            x1 = min(img.width, max(l["bbox"][0] + l["bbox"][2] for l in band) + PAD)
            cctx = img.crop((x0, y0, x1, y1))
            out2 = OUT / f"{page_id}_l{line_no:03d}_ctx.jpg"
            cctx.save(out2, quality=92)
            print(out2.name, cctx.size)


if __name__ == "__main__":
    main()
