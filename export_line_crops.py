#!/usr/bin/env python
"""KINSHIP-HTR-BENCH-01: export line crops + review payload from batch output.

Reads transcriptions/*.json (polyscriptor batch output) and the source scans,
writes per-line crop images, bbox-overlay page renders, and payload.json
feeding the human review interface.

Usage: export_line_crops.py <pages-dir> <batch-out-dir> <review-dir>
"""
import json
import sys
from pathlib import Path
from PIL import Image, ImageDraw

PAD = 10  # px padding around bbox in crops


def main(pages_dir: Path, out_dir: Path, review_dir: Path) -> None:
    crops_dir = review_dir / 'crops'
    overlay_dir = review_dir / 'overlays'
    crops_dir.mkdir(parents=True, exist_ok=True)
    overlay_dir.mkdir(parents=True, exist_ok=True)

    payload = []
    for tj in sorted((out_dir / 'transcriptions').glob('*.json')):
        page = json.load(open(tj, encoding='utf-8'))
        img_name = page['image']
        img_path = pages_dir / img_name
        img = Image.open(img_path)
        img.load()

        # bbox overlay render (1:3 downscale)
        scale = 1 / 3
        ov = img.copy()
        d = ImageDraw.Draw(ov)
        for ln in page['lines']:
            x, y, w, h = ln['bbox']
            d.rectangle([x - 3, y - 3, x + w + 3, y + h + 3], outline='red', width=3)
        ov = ov.resize((round(ov.width * scale), round(ov.height * scale)))
        ov.save(overlay_dir / (img_path.stem + '.jpg'), quality=80)

        for ln in page['lines']:
            x, y, w, h = ln['bbox']
            crop = img.crop((max(0, x - PAD), max(0, y - PAD),
                             min(img.width, x + w + PAD), min(img.height, y + h + PAD)))
            crop_name = f"{img_path.stem}_l{ln['line_number']:03d}.jpg"
            crop.save(crops_dir / crop_name, quality=92)
            payload.append({
                'page': img_path.stem,
                'line_number': ln['line_number'],
                'bbox_xywh': ln['bbox'],
                'crop': f'crops/{crop_name}',
                'model_text': ln['text'],
                'model_confidence': round(ln['confidence'], 4),
                'provisional_reference': '',
                'reference_provenance': 'none',
                'verified': False,
            })

    (review_dir / 'payload.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f"exported {len(payload)} lines from {len(list((out_dir / 'transcriptions').glob('*.json')))} pages")


if __name__ == '__main__':
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
