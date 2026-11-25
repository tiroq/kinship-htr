#!/usr/bin/env python
"""KINSHIP-HTR-BENCH-01: build self-contained human review interface.

Embeds payload.json (line crops referenced as relative paths, text, model
output, provisional reference, verification state) into review.html so it
works from file:// without a server. Reviewer edits references, marks lines
verified, exports reviewed.json via the download button.

Usage: build_review.py <review-dir>
"""
import json
import sys
from pathlib import Path

TEMPLATE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>KINSHIP-HTR-BENCH-01 — line review</title>
<style>
 body{font-family:-apple-system,sans-serif;margin:0;background:#f5f5f5;color:#222}
 header{position:sticky;top:0;background:#fff;border-bottom:1px solid #ccc;padding:8px 16px;z-index:5;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
 header b{font-size:14px}
 .stats{font-size:13px;color:#555}
 button{padding:6px 14px;border:1px solid #888;border-radius:6px;background:#fff;cursor:pointer}
 button.primary{background:#2563eb;color:#fff;border-color:#2563eb}
 select{padding:4px}
 main{padding:16px;display:flex;flex-direction:column;gap:12px}
 .card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px 14px;display:grid;grid-template-columns:340px 1fr;gap:14px}
 .card.verified{border-left:5px solid #16a34a}
 .card.unverified{border-left:5px solid #d97706}
 .crop img{max-width:330px;max-height:120px;border:1px solid #eee;image-rendering:auto}
 .meta{font-size:12px;color:#777;margin-bottom:4px}
 .row{display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap}
 .model{font-family:ui-monospace,monospace;font-size:13px;color:#444;background:#f9f9f9;padding:2px 6px;border-radius:4px}
 textarea{width:100%;min-height:44px;font-family:ui-monospace,monospace;font-size:14px;border:1px solid #bbb;border-radius:4px;padding:4px 6px}
 label.ck{display:flex;gap:6px;align-items:center;font-size:13px}
 .page-h{background:#e5e7eb;padding:4px 10px;border-radius:6px;font-weight:600;margin-top:8px}
 .conf-low{color:#b91c1c}.conf-mid{color:#b45309}.conf-hi{color:#15803d}
</style></head><body>
<header>
 <b>KINSHIP-HTR-BENCH-01 — сверка строк (human verification)</b>
 <span class="stats" id="stats"></span>
 <span style="flex:1"></span>
 <select id="filterPage"><option value="">все страницы</option></select>
 <select id="filterState">
   <option value="">все строки</option>
   <option value="unverified">не сверенные</option>
   <option value="verified">сверенные</option>
 </select>
 <button onclick="exportJson()" class="primary">Export reviewed.json</button>
</header>
<main id="main"></main>
<script>
const DATA = __PAYLOAD__;
const LSKEY = 'htr-bench-01-review';
// merge localStorage overrides
try { const saved = JSON.parse(localStorage.getItem(LSKEY) || '{}');
  DATA.forEach(l => { const k = l.page + '/' + l.line_number;
    if (saved[k]) Object.assign(l, saved[k]); }); } catch (e) {}
function persist() {
  const saved = {};
  DATA.forEach(l => { const k = l.page + '/' + l.line_number;
    if (l.reference !== undefined || l.verified || l.comment)
      saved[k] = {reference: l.reference || '', verified: !!l.verified, comment: l.comment || ''}; });
  localStorage.setItem(LSKEY, JSON.stringify(saved));
  renderStats();
}
function stateOf(l) { return l.verified ? 'verified' : 'unverified'; }
function renderStats() {
  const v = DATA.filter(l => l.verified).length;
  const withRef = DATA.filter(l => (l.reference || '').trim()).length;
  document.getElementById('stats').textContent =
    `строк: ${DATA.length} · со сверкой: ${v} · с эталоном: ${withRef}`;
}
function render() {
  const fp = document.getElementById('filterPage').value;
  const fs = document.getElementById('filterState').value;
  const pages = [...new Set(DATA.map(l => l.page))];
  const sel = document.getElementById('filterPage');
  if (sel.options.length === 1)
    pages.forEach(p => sel.add(new Option(p, p)));
  const main = document.getElementById('main');
  main.innerHTML = '';
  let lastPage = null;
  DATA.forEach((l, i) => {
    if (fp && l.page !== fp) return;
    if (fs && stateOf(l) !== fs) return;
    if (l.page !== lastPage) {
      lastPage = l.page;
      const h = document.createElement('div');
      h.className = 'page-h'; h.textContent = 'Дело ' + l.page; main.appendChild(h);
    }
    const c = l.confClass = l.model_confidence < .5 ? 'conf-low'
      : l.model_confidence < .8 ? 'conf-mid' : 'conf-hi';
    const card = document.createElement('div');
    card.className = 'card ' + stateOf(l);
    card.innerHTML = `
      <div class="crop"><div class="meta">${l.page} · строка ${l.line_number} · bbox [${l.bbox_xywh.join(', ')}]</div>
        <img src="${l.crop}" alt="line ${l.line_number}"></div>
      <div>
        <div class="row"><span class="meta">модель (conf <b class="${c}">${(l.model_confidence*100).toFixed(1)}%</b>):</span>
          <span class="model">${escapeHtml(l.model_text)}</span></div>
        ${l.provisional_reference ? `<div class="row"><span class="meta">черновой вариант (AI, непроверенный):</span>
          <span class="model">${escapeHtml(l.provisional_reference)}</span></div>` : ''}
        <div class="row" style="margin-top:6px">
          <textarea id="ref${i}" placeholder="дословное чтение строки (дипломатическое, как в документе)">${escapeHtml(l.reference || '')}</textarea>
        </div>
        <div class="row">
          <label class="ck"><input type="checkbox" id="ck${i}" ${l.verified ? 'checked' : ''}> проверено человеком (верно / эталон принят)</label>
          <input id="cm${i}" placeholder="примечание (необязательно)" value="${escapeHtml(l.comment || '')}" style="flex:1;border:1px solid #ccc;border-radius:4px;padding:4px 6px;font-size:12px">
          ${l.provisional_reference ? `<button onclick="copyProv(${i})">← черновой</button>` : ''}
        </div>
      </div>`;
    card.querySelector(`#ref${i}`).addEventListener('input', e => {
      l.reference = e.target.value; persist(); });
    card.querySelector(`#ck${i}`).addEventListener('change', e => {
      l.verified = e.target.checked; persist(); render(); });
    card.querySelector(`#cm${i}`).addEventListener('input', e => {
      l.comment = e.target.value; persist(); });
    card.copyProv = () => {
      document.getElementById('ref' + i).value = l.provisional_reference || '';
      l.reference = l.provisional_reference || ''; persist(); };
    main.appendChild(card);
  });
  renderStats();
}
function copyProv(i) {
  const l = DATA[i];
  document.getElementById('ref' + i).value = l.provisional_reference || '';
  l.reference = l.provisional_reference || ''; persist();
}
function escapeHtml(s) {
  return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function exportJson() {
  const out = DATA.map(l => ({
    page: l.page, line_number: l.line_number, bbox_xywh: l.bbox_xywh,
    crop: l.crop, model_text: l.model_text, model_confidence: l.model_confidence,
    provisional_reference: l.provisional_reference,
    reference: l.reference || '',
    comment: l.comment || '', verified: !!l.verified }));
  const blob = new Blob([JSON.stringify(out, null, 1)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'reviewed.json'; a.click();
}
document.getElementById('filterPage').addEventListener('change', render);
document.getElementById('filterState').addEventListener('change', render);
render();
</script></body></html>
"""


def main(review_dir: Path) -> None:
    payload = json.loads((review_dir / 'payload.json').read_text(encoding='utf-8'))
    html = TEMPLATE.replace('__PAYLOAD__', json.dumps(payload, ensure_ascii=False))
    (review_dir / 'review.html').write_text(html, encoding='utf-8')
    print(f"review.html written: {len(payload)} lines, "
          f"{len(html)/1024:.0f} KiB")


if __name__ == '__main__':
    main(Path(sys.argv[1]))
