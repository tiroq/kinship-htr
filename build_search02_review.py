#!/usr/bin/env python3
"""KINSHIP-SEARCH-02 — build source crops + the compact HTML review page.

Reads work/discovery/kinship-search02/kinship-search02.json (top groups) and
the original scans (read-only) and emits:
  crops/<group_id>-region.png   household region (native scale)
  crops/<group_id>-key.png      first key name line at 2.5x
  kinship-search02-review.html  compact human-review page

The page is review infrastructure: every reading shown is UNVERIFIED OCR;
per-group «проверено человеком» fields autosave to localStorage and export
as JSON.  Nothing is verified by this build.

Run:  htrbench-python build_search02_review.py
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

from PIL import Image

BENCH = Path("/Users/mysterx/dev/kinship-htr-bench")
DATA = BENCH / "work" / "discovery" / "kinship-search02" / "kinship-search02.json"
OUT_DIR = BENCH / "work" / "discovery" / "kinship-search02"
CROPS = OUT_DIR / "crops"
OUT_HTML = OUT_DIR / "kinship-search02-review.html"

PAT_BADGE = {
    "P1": ("Иван голова/отец → Пётр его сын", "#2563eb"),
    "P2": ("Пётр Ивановъ (отчество)", "#7c3aed"),
    "P3": ("Иван + Пётр в одном дворе, связь неясна", "#b45309"),
    "P4": ("Пётр отец Ивана / братья", "#0f766e"),
    "P5": ("Иван без Петра (сын мог родиться позже)", "#64748b"),
}


def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def crop_region(img: Image.Image, bboxes: list[list[int]], pad: int = 40):
    if not bboxes:
        return None
    x0 = max(0, min(b[0] for b in bboxes) - pad)
    y0 = max(0, min(b[1] for b in bboxes) - pad)
    x1 = min(img.width, max(b[0] + b[2] for b in bboxes) + pad)
    y1 = min(img.height, max(b[1] + b[3] for b in bboxes) + pad)
    if x1 - x0 < 30 or y1 - y0 < 20:
        return None
    return img.crop((x0, y0, x1, y1))


def build_crops(groups: list[dict]) -> dict[str, dict]:
    made = {}
    cache: dict[str, Image.Image] = {}
    for g in groups:
        gid = g["group_id"]
        src = g["links"].get("source_image")
        if not src or not Path(src).exists():
            continue
        if src not in cache:
            cache[src] = Image.open(src)
        img = cache[src]
        hh_bboxes = [p["bbox"] for p in g["persons"]]
        ctx_bboxes = [c["bbox"] for c in g.get("context_lines", [])]
        region = crop_region(img, hh_bboxes, pad=50)
        files = {}
        if region is not None and region.width > 0:
            fp = CROPS / f"{gid}-region.png"
            region.save(fp)
            files["region"] = fp.name
        key_p = next((p for p in g["persons"] if p["given_match"]), None)
        if key_p is not None:
            k = crop_region(img, [key_p["bbox"]], pad=26)
            if k is not None:
                k = k.resize((int(k.width * 2.5), int(k.height * 2.5)),
                             Image.LANCZOS)
                fp = CROPS / f"{gid}-key.png"
                k.save(fp)
                files["key"] = fp.name
        made[gid] = files
    return made


def persons_table(g: dict) -> str:
    rows = []
    for p in g["persons"]:
        gm = p["given_match"] or {}
        tier = gm.get("tier", "")
        name_cls = "name-strong" if gm.get("strong") else (
            "name-fuzzy" if tier == "fuzzy" else "")
        patr = esc(p["patronymic_raw_ocr"] or "")
        pm = p["patronymic_match"] or {}
        if pm:
            patr += f" <span class='pm'>{pm['name']}→отчество</span>"
        fz = f" ({gm.get('fuzz')})" if gm.get("fuzz") else ""
        rows.append(
            f"<tr><td>{esc(p['role'])}</td>"
            f"<td class='{name_cls}'>{esc(p['given_raw_ocr'] or '—')}"
            f"{fz}</td><td>{patr}</td>"
            f"<td>{p['age'] if p.get('age') is not None else '—'}</td>"
            f"<td>{p['chislo'] if p.get('chislo') is not None else '—'}</td>"
            f"<td>{esc(round(p['ocr_confidence'], 2)
                        if p.get('ocr_confidence') is not None else '—')}</td></tr>")
    return ("<table class='persons'><tr><th>роль</th><th>имя (OCR)</th>"
            "<th>отчество (OCR)</th><th>возраст</th><th>№</th>"
            "<th>conf</th></tr>" + "".join(rows) + "</table>")


def context_block(g: dict) -> str:
    hh_lines = {p["line_number"] for p in g["persons"]}
    out = []
    for c in g.get("context_lines", []):
        t = c["text_raw"] or "〔строка не распознана〕"
        cls = []
        if c["line_number"] in hh_lines:
            cls.append("hh")
        for tag in c.get("tags", []):
            if tag in ("village", "year", "closing"):
                cls.append("hd")
            elif tag == "number":
                cls.append("num")
            elif tag == "empty-ocr":
                cls.append("empty")
        out.append(f"<div class='ctx {' '.join(cls)}'>"
                   f"<span class='ln'>l{c['line_number']}</span>"
                   f"{esc(t)}</div>")
    return "<div class='ctxwrap'>" + "".join(out) + "</div>"


def group_card(g: dict, crops: dict) -> str:
    gid = g["group_id"]
    badges = " ".join(
        f"<span class='badge' style='border-color:{PAT_BADGE[p][1]}'>"
        f"{p}: {PAT_BADGE[p][0]}</span>" for p in g["patterns"])
    year = g["archival_year"]
    yv = year.get("value") or year.get("range") or "?"
    sec = g["settlement"]["section"]
    sec_t = (f"Волченокъ ({sec['classification']})" if sec
             else esc(g["settlement"]["village_ocr"] or
                      "поселение не определено (OCR)"))
    yr = esc(yv)
    if year.get("value") and "OUTSIDE" in (year.get("status") or ""):
        yr += " ⚠ вне диапазона дела"
    crop_html = ""
    cf = crops.get(gid, {})
    if cf.get("region"):
        crop_html += (f"<img src='crops/{cf['region']}' "
                      f"alt='изображение двора {esc(gid)}'>")
    if cf.get("key"):
        crop_html += (f"<img src='crops/{cf['key']}' class='key' "
                      f"alt='строка с именем {esc(gid)}'>")
    unc = "".join(f"<li>{esc(u)}</li>" for u in g.get("uncertainties", []))
    rec = ""
    if g.get("recurrence"):
        rec = (f"<div class='rec'> ⟳ возможное повторение семьи в другие "
               f"годы: {esc(', '.join(g['recurrence']['partner_groups']))} "
               f"— <b>НЕ установление личности</b>, только повод для "
               f"проверки</div>")
    ev = "".join(f"<li>{esc(e)}</li>" for e in g.get("relation_evidence", []))
    links = g["links"]
    return f"""
<section class='grp' id='{esc(gid)}' data-gid='{esc(gid)}'>
<h3>#{g['rank']} <span class='score'>{g['score']}</span> {esc(gid)}</h3>
<div class='meta'>{badges}</div>
<div class='meta'>дело {esc(g['case'])} · скан {esc(g['page_id'])}
 столбец {esc(g['column'])} · год ~{yr} (OCR, непроверено) · {sec_t}</div>
{rec}
<div class='cols'><div>
<h4>Состав двора (OCR, непроверено)</h4>
{persons_table(g)}
<h4>Связи (доказательная база шаблона)</h4>
<ul class='ev'>{ev}</ul>
<h4>Неясности</h4>
<ul class='unc'>{unc or '<li>нет автоматических флагов — всё равно нужна проверка изображений</li>'}</ul>
<div class='verify'>
<label><input type='checkbox' class='vchk'> проверено человеком</label>
<input type='text' class='vnotes' placeholder='заметки проверяющего'>
</div>
</div><div>
<h4>Контекст ±8 строк (та же страница реестра)</h4>
{context_block(g)}
{f"<div class='crops'>{crop_html}</div>" if crop_html else "<div class='crops'>нет кропа (скан недоступен)</div>"}
<div class='links'>исходник: <code>{esc(links.get('source_image'))}</code>
 · API: <a href='{esc(links.get('crop_first_key_line') or '#')}'>crop</a>
 <a href='/api/image/{esc(g['page_id'])}'>page</a></div>
</div></div>
</section>"""


def main() -> int:
    CROPS.mkdir(parents=True, exist_ok=True)
    d = json.loads(DATA.read_text())
    groups = d["top_groups"]
    t0 = time.time()
    crops = build_crops(groups)
    print(f"crops: {len(crops)} groups -> {CROPS}")

    tot_i, tot_p = d["totals"]["иван"], d["totals"]["пётр"]
    pc = d["pattern_group_counts"]
    summary = f"""
<table class='sum'>
<tr><th>показатель</th><th>Иван</th><th>Пётр</th></tr>
<tr><td>строк с именем (точное/склеенное)</td>
<td>{tot_i['given_name_lines_exact_or_glued']}</td>
<td>{tot_p['given_name_lines_exact_or_glued']}</td></tr>
<tr><td>+ сильные OCR-искажения (сходство ≥80)</td>
<td>{tot_i['given_name_lines_fuzzy_strong_80plus']}</td>
<td>{tot_p['given_name_lines_fuzzy_strong_80plus']}</td></tr>
<tr><td>+ слабые кандидаты (74–79, в основном шум)</td>
<td>{tot_i['given_name_lines_fuzzy_weak_74_79']}</td>
<td>{tot_p['given_name_lines_fuzzy_weak_74_79']}</td></tr>
<tr><td>строк только с отчеством (Ивановъ/Петровъ-формы)</td>
<td>{tot_i['patronymic_only_lines']}</td>
<td>{tot_p['patronymic_only_lines']}</td></tr>
<tr><td>всего вхождений отчеств-форм</td>
<td>{tot_i['patronymic_form_occurrences']}</td>
<td>{tot_p['patronymic_form_occurrences']}</td></tr>
</table>
<table class='sum'>
<tr><th>шаблон</th><th>дворов</th></tr>
<tr><td>P1 Иван→сын Пётр</td><td>{pc.get('P1', 0)}</td></tr>
<tr><td>P2 Пётр Ивановъ</td><td>{pc.get('P2', 0)}</td></tr>
<tr><td>P3 совместное проживание, связь неясна</td><td>{pc.get('P3', 0)}</td></tr>
<tr><td>P4 Пётр→сын Иван / братья</td><td>{pc.get('P4', 0)}</td></tr>
<tr><td>P5 Иван без Петра</td><td>{pc.get('P5', 0)}</td></tr>
</table>
<p>Всего дворов-кандидатов: {d['distinct_candidate_groups']}
 (в проверенных секциях Волченокъ: {d['groups_in_volchenok_sections']}).</p>"""

    cards = "".join(group_card(g, crops) for g in groups)
    page = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>KINSHIP-SEARCH-02 · Иван/Пётр — обзор кандидатов</title>
<style>
:root {{ color-scheme: light; }}
body {{ font: 14px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif;
  margin: 0 auto; max-width: 1250px; padding: 18px; color: #111; }}
h1 {{ font-size: 20px; }} h3 {{ font-size: 16px; margin: 4px 0; }}
h4 {{ font-size: 13px; margin: 10px 0 4px; color: #444; }}
.sum {{ border-collapse: collapse; margin: 6px 12px 6px 0; display:
  inline-table; vertical-align: top; }}
.sum th, .sum td {{ border: 1px solid #ccc; padding: 3px 9px;
  font-size: 13px; text-align: left; }}
.grp {{ border: 1px solid #ddd; border-radius: 8px; margin: 14px 0;
  padding: 10px 14px; }}
.badge {{ border: 1.5px solid; border-radius: 6px; padding: 1px 8px;
  margin-right: 6px; font-size: 12px; }}
.score {{ background: #eef2ff; border-radius: 5px; padding: 0 7px; }}
.meta {{ color: #333; margin: 3px 0; font-size: 13px; }}
.cols {{ display: flex; gap: 18px; flex-wrap: wrap; }}
.cols > div {{ flex: 1 1 420px; }}
.persons {{ border-collapse: collapse; font-size: 12.5px; width: 100%; }}
.persons th, .persons td {{ border: 1px solid #e2e2e2; padding: 2px 6px; }}
.name-strong {{ background: #e7f5e7; font-weight: 600; }}
.name-fuzzy {{ background: #fdf3e0; }}
.pm {{ color: #7c3aed; font-size: 11px; }}
.ctxwrap {{ font: 12.5px/1.5 "SF Mono", Menlo, Consolas, monospace;
  border: 1px solid #eee; background: #fafafa; padding: 6px;
  max-height: 340px; overflow-y: auto; }}
.ctx {{ white-space: pre-wrap; }}
.ctx .ln {{ color: #999; margin-right: 7px; }}
.ctx.hh {{ background: #eef7ff; }}
.ctx.hd {{ color: #b45309; }}
.ctx.num {{ color: #666; }}
.ctx.empty {{ color: #c33; font-style: italic; }}
.crops img {{ max-width: 100%; border: 1px solid #ccc; margin: 4px 0;
  display: block; }}
.crops img.key {{ max-width: 70%; }}
.ev li, .unc li {{ font-size: 12.5px; margin: 2px 0; }}
.rec {{ background: #fef9c3; border: 1px solid #eab308; border-radius: 6px;
  padding: 4px 9px; font-size: 12.5px; margin: 5px 0; }}
.verify {{ margin-top: 8px; display: flex; gap: 10px; align-items: center;
  font-size: 13px; }}
.verify input[type=text] {{ flex: 1; }}
.vdone {{ outline: 3px solid #22c55e; }}
.links {{ font-size: 11.5px; color: #555; word-break: break-all; }}
footer {{ margin: 20px 0; font-size: 12px; color: #555; }}
</style></head><body>
<h1>KINSHIP-SEARCH-02 · поиск семей Ивана и Петра по 4 делам (253, 255,
256, 258)</h1>
<p>ЦГА Москвы ф.597 оп.1, исповедные ведомости села Протасьева.
<b>Всё показанное — непроверенный OCR</b>: каждое имя и возраст требует
прочтения по кропу/скану. Отчества (Ивановъ, Петровъ) учитываются отдельно
от личных имён; «Иван Петровъ» ≠ «Пётр Ивановъ». Фамилии владельцев
(Головкина и т.п.) никогда не считаются родством. Присвоение личностей
не выполняется.</p>
{summary}
<h2>Топ-{len(groups)} дворов-кандидатов</h2>
{cards}
<footer>Сгенерировано {esc(d['ran_at'])} · {esc(round(time.time()-t0,1))}s ·
JSON: <code>kinship-search02.json</code> · кропы: <code>crops/</code> ·
server API: <code>/api/crop/&lt;line&gt;?pad=160</code> (запуск:
<code>htrbench-python -m htr_search.server 8765</code>)<br>
Чекбоксы и заметки сохраняются локально (localStorage); кнопка ниже
выгружает проверенное как JSON.</footer>
<button onclick="exp()">Выгрузить проверенное (JSON)</button>
<script>
function key() {{ return 'kinship-search02-review'; }}
function load() {{
  try {{
    const st = JSON.parse(localStorage.getItem(key()) || '{{}}');
    document.querySelectorAll('.grp').forEach(el => {{
      const g = el.dataset.gid, s = st[g];
      if (!s) return;
      el.querySelector('.vchk').checked = !!s.done;
      el.querySelector('.vnotes').value = s.notes || '';
      if (s.done) el.classList.add('vdone');
    }});
  }} catch (e) {{}}
}}
function save() {{
  const st = {{}};
  document.querySelectorAll('.grp').forEach(el => {{
    st[el.dataset.gid] = {{
      done: el.querySelector('.vchk').checked,
      notes: el.querySelector('.vnotes').value
    }};
    el.classList.toggle('vdone', el.querySelector('.vchk').checked);
  }});
  localStorage.setItem(key(), JSON.stringify(st));
}}
function exp() {{
  const st = JSON.parse(localStorage.getItem(key()) || '{{}}');
  const out = Object.entries(st).map(([gid, s]) => ({{
    group_id: gid, human_verified: s.done, notes: s.notes,
    provenance: 'HUMAN REVIEW of KINSHIP-SEARCH-02 candidates'
  }}));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob(
    [JSON.stringify(out, null, 1)], {{type: 'application/json'}}));
  a.download = 'kinship-search02-reviewed.json';
  a.click();
}}
document.addEventListener('change', save);
load();
</script></body></html>"""
    OUT_HTML.write_text(page)
    print(f"html -> {OUT_HTML} ({len(groups)} groups)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
