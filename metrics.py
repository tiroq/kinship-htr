#!/usr/bin/env python
"""KINSHIP-HTR-BENCH-01: metrics from reviewed payload.

Scores ONLY lines with verified=true (independent human-verified ground truth).
- Strict diplomatic CER: Levenshtein distance / len(reference), no normalization
  (case, ѣ/і/ѳ, punctuation and spacing all count as written).
- Genealogy-field heuristic metrics on the same lines:
    * name-token accuracy: reference alphabetic tokens (incl. 'ѣіѳъ') exactly
      present in model text / total reference tokens
    * digit-group accuracy: reference digit groups present in model text / total
- Per-line and aggregate (micro/macro) outputs as JSON + markdown table.

Usage: metrics.py <reviewed.json> <out-prefix>
"""
import json
import re
import sys
from pathlib import Path


def levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


ALPHA = re.compile(r'[А-Яа-яѢѣІіѲѳ ъЪ]+')
DIGITS = re.compile(r'\d+')


def field_metrics(ref: str, hyp: str) -> dict:
    ref_words = [w for w in ALPHA.findall(ref) if w.strip()]
    hyp_words = set(ALPHA.findall(hyp))
    ref_digits = DIGITS.findall(ref)
    hyp_digits = set(DIGITS.findall(hyp))
    return {
        'ref_name_tokens': len(ref_words),
        'name_hits': sum(1 for w in ref_words if w in hyp_words),
        'ref_digit_groups': len(ref_digits),
        'digit_hits': sum(1 for d in ref_digits if d in hyp_digits),
    }


def main(reviewed: Path, prefix: Path) -> None:
    data = json.loads(reviewed.read_text(encoding='utf-8'))
    ver = [l for l in data if l.get('verified') and (l.get('reference') or '').strip()]
    per_line, name_tok, name_hit, dg, dgh = [], 0, 0, 0, 0
    for l in ver:
        ref, hyp = l['reference'], l['model_text']
        dist = levenshtein(ref, hyp)
        cer = dist / max(len(ref), 1)
        fm = field_metrics(ref, hyp)
        name_tok += fm['ref_name_tokens']; name_hit += fm['name_hits']
        dg += fm['ref_digit_groups']; dgh += fm['digit_hits']
        per_line.append({
            'page': l['page'], 'line_number': l['line_number'],
            'reference': ref, 'model_text': hyp,
            'levenshtein': dist, 'ref_len': len(ref), 'cer': round(cer, 4),
            'confidence': l['model_confidence'],
        })
    micro_cer = (sum(p['levenshtein'] for p in per_line) /
                 max(sum(p['ref_len'] for p in per_line), 1)) if per_line else None
    macro_cer = (sum(p['cer'] for p in per_line) / len(per_line)) if per_line else None
    out = {
        'verified_lines': len(ver),
        'total_lines_payload': len(data),
        'strict_diplomatic_cer': {
            'micro': round(micro_cer, 4) if micro_cer is not None else None,
            'macro': round(macro_cer, 4) if macro_cer is not None else None,
        },
        'genealogy_field_metrics': {
            'name_token_accuracy': round(name_hit / name_tok, 4) if name_tok else None,
            'name_tokens': name_tok,
            'digit_group_accuracy': round(dgh / dg, 4) if dg else None,
            'digit_groups': dg,
        },
        'per_line': per_line,
    }
    prefix.with_suffix('.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')

    md = [f"# KINSHIP-HTR-BENCH-01 metrics — {len(ver)} verified lines\n",
          f"- strict diplomatic CER (micro): {out['strict_diplomatic_cer']['micro']}",
          f"- strict diplomatic CER (macro): {out['strict_diplomatic_cer']['macro']}",
          f"- name-token accuracy: {out['genealogy_field_metrics']['name_token_accuracy']} "
          f"({name_hit}/{name_tok})",
          f"- digit-group accuracy: {out['genealogy_field_metrics']['digit_group_accuracy']} "
          f"({dgh}/{dg})\n",
          "| page | line | CER | conf | reference | model |", "|---|---|---|---|---|---|"]
    for p in per_line:
        md.append(f"| {p['page']} | {p['line_number']} | {p['cer']:.3f} | "
                  f"{p['confidence']:.2f} | {p['reference'][:40]} | {p['model_text'][:40]} |")
    prefix.with_suffix('.md').write_text('\n'.join(md), encoding='utf-8')
    print(json.dumps({k: v for k, v in out.items() if k != 'per_line'},
                     ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main(Path(sys.argv[1]), Path(sys.argv[2]))
