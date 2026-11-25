"""Stage B — lightweight layout/missing-header recovery (KINSHIP-HTR-SEARCH-01).

Detects ink bands NOT covered by the kraken-blla line boxes (upper-page
headings, settlement section titles, marginal notes, other missed regions)
via Otsu binarization + row/column projection profiles, then recognizes each
candidate band directly with the loaded CRNN-CTC engine.  Cheap by design:
no second neural segmentation pass, a bounded number of bands per page.

Duplicate suppression: candidate bands overlapping an existing OCR bbox with
IoU > 0.30 (or centre-inside) are dropped; stage-B results are deduped among
themselves the same way.  All results carry provenance 'stage-b-gap-recovery'
and are stored separately from the main OCR lines.

This is a recall net, not a layout-complete solution: absence of stage-B
lines on a page does NOT mean the page layout is fully covered; visual
verification stays mandatory.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

MAX_BANDS = 6            # bound cost per page
BAND_MIN_H, BAND_MAX_H = 18, 150
BAND_MIN_W = 60
ROW_INK_FRAC = 0.004     # rows with >= this fraction of uncovered ink join a band
GAP_MERGE = 18           # px; smaller vertical gaps merge bands
COL_GAP = 80            # px; column split inside a band
PAD = 10                # crop padding
TOP_FRAC, MARGIN_FRAC = 0.13, 0.09


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    return inter / (aw * ah + bw * bh - inter)


def _centre_inside(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return bx <= ax + aw / 2 <= bx + bw and by <= ay + ah / 2 <= by + bh


def _find_bands(ink: np.ndarray, W: int, H: int):
    """Row bands of uncovered ink, then column clusters inside each band."""
    row_profile = ink.sum(axis=1)
    thresh = max(8.0, ROW_INK_FRAC * W)
    hot = row_profile >= thresh
    bands, start = [], None
    for y in range(H):
        if hot[y] and start is None:
            start = y
        elif not hot[y] and start is not None:
            bands.append((start, y))
            start = None
    if start is not None:
        bands.append((start, H))
    # merge close bands
    merged = []
    for y0, y1 in bands:
        if merged and y0 - merged[-1][1] < GAP_MERGE:
            merged[-1] = (merged[-1][0], y1)
        else:
            merged.append((y0, y1))

    out = []
    for y0, y1 in merged:
        h = y1 - y0
        if not (BAND_MIN_H <= h <= BAND_MAX_H):
            continue
        seg = ink[y0:y1]
        col = seg.sum(axis=0)
        colhot = col >= 3
        # column clusters
        clusters, cs = [], None
        for x in range(W):
            if colhot[x] and cs is None:
                cs = x
            elif not colhot[x] and cs is not None:
                clusters.append((cs, x))
                cs = None
        if cs is not None:
            clusters.append((cs, W))
        mc = []
        for x0, x1 in clusters:
            if x1 - x0 < GAP_MERGE:
                continue
            if mc and x0 - mc[-1][1] < COL_GAP:
                mc[-1] = (mc[-1][0], x1)
            else:
                mc.append((x0, x1))
        for x0, x1 in mc:
            w = x1 - x0
            if w < BAND_MIN_W:
                continue
            out.append((x0, y0, w, h, int(seg[:, x0:x1].sum())))
    return out


def _rank(bands, W, H):
    def score(b):
        x, y, w, h, ink = b
        s = float(ink)
        if y < TOP_FRAC * H:              # upper-page heading
            s *= 2.0
        if x < MARGIN_FRAC * W or x + w > (1 - MARGIN_FRAC) * W:
            s *= 1.6                      # marginal content
        return s
    return sorted(bands, key=score, reverse=True)[:MAX_BANDS]


def recover_uncovered(engine, image: Image.Image, existing_bboxes, min_h=15, min_w=16):
    """image: PIL grayscale page. existing_bboxes: list of (x,y,w,h) already OCR'd.

    Returns {"provenance":…, "bands_examined": n, "lines": [ {line_number,
    text, confidence, bbox}, … ]} with bbox in full-page image coordinates.
    """
    W, H = image.size
    arr = np.asarray(image)
    # Otsu threshold via 256-bin histogram (no skimage dependency in hot path)
    hist = np.bincount(arr.ravel(), minlength=256).astype(np.float64)
    total, sum_all = hist.sum(), float(np.dot(np.arange(256), hist))
    w_b, sum_b, best, thr = 0.0, 0.0, -1.0, 128
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b, m_f = sum_b / w_b, (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best:
            best, thr = var, t
    ink = arr < thr

    # mask out a slightly expanded version of existing boxes
    covered = np.zeros_like(ink)
    for (x, y, w, h) in existing_bboxes:
        x0, y0 = max(0, x - 4), max(0, y - 4)
        x1, y1 = min(W, x + w + 4), min(H, y + h + 4)
        covered[y0:y1, x0:x1] = True
    uncovered = ink & ~covered

    bands = _find_bands(uncovered, W, H)
    ranked = _rank(bands, W, H)

    lines, examined = [], 0
    for x, y, w, h, inkpx in ranked:
        box = (x, y, w, h)
        if any(_iou(box, e) > 0.30 or _centre_inside(box, e) for e in existing_bboxes):
            continue
        if any(_iou(box, tuple(l["bbox"])) > 0.30 for l in lines):
            continue
        x0, y0 = max(0, x - PAD), max(0, y - PAD)
        x1, y1 = min(W, x + w + PAD), min(H, y + h + PAD)
        cw, ch = x1 - x0, y1 - y0
        if ch < min_h or cw < min_w:
            continue
        examined += 1
        crop = image.crop((x0, y0, x1, y1))
        try:
            res = engine.transcribe_lines([np.asarray(crop)])[0]
        except Exception:
            continue
        text = (res.text or "").strip()
        if not text or text == "[ERROR]":
            continue
        lines.append({
            "line_number": 1000 + len(lines),
            "text": text,
            "confidence": float(res.confidence) if getattr(res, "confidence", None) else None,
            "bbox": [x, y, w, h],
            "band_ink_px": inkpx,
        })
    return {
        "provenance": "stage-b-gap-recovery (v1 projection-profile, Otsu, direct CRNN-CTC)",
        "bands_examined": examined,
        "lines": lines,
    }
