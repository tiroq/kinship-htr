#!/usr/bin/env python3
"""KINSHIP-HTR-FAMILY-01 Phase C — build the household human-review interface.

Reads work/households/volchenok-258-households.json and emits a fully
self-contained work/households/households-review.html:
  * per household: source crop image(s), OCR suggestions, visual drafts,
    per-person fields for a HUMAN-verified diplomatic transcription + age,
    a «проверено человеком» checkbox and a notes field;
  * nothing is treated as verified until a human ticks it;
  * progress autosaves to localStorage; «Export verified JSON» downloads
    exactly the fields a human filled in.

Run:  htrbench-python build_households_review.py
"""
from __future__ import annotations

import json
from pathlib import Path

BENCH = Path(__file__).resolve().parent
DATA = BENCH / "work" / "households" / "volchenok-258-households.json"
OUT = BENCH / "work" / "households" / "households-review.html"


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;")


def person_block(hid: str, i: int, p: dict) -> str:
    nm = p.get("name", {}) or {}
    age = p.get("age", {}) or {}
    chis = p.get("chislo", {}) or {}
    ocr = esc(nm.get("ocr") or "—")
    visual = esc(nm.get("visual") or "—")
    status = esc(nm.get("status") or "")
    transcript = esc(nm.get("transcript_human") or "")
    return f"""
<div class="person" data-person="{i}">
 <div class="labels">
  <span class="role">{esc(p.get('role') or '')}</span>
  <span class="tag ocr">OCR (unverified suggestion)</span>
  <span class="tag vis">VISUAL draft (AI-assisted, NOT human)</span>
 </div>
 <div class="src">
  <div class="s ocr">{ocr}</div>
  <div class="s vis">{visual}</div>
  {f'<div class="s hum">HUMAN transcript (case file): {transcript}</div>' if transcript else ''}
  {f'<div class="s warn">{status}</div>' if status else ''}
 </div>
 <div class="inputs">
  <label>человеческая выверка (дипломатическая, как написано)
   <input name="{hid}-p{i}-verified_text" placeholder="впишите, что читаете на снимке"></label>
  <label>возраст
   <input name="{hid}-p{i}-age" size="4" placeholder="{esc(str(age.get('visual') or age.get('ocr') or ''))}"></label>
  <label>число (столбец)
   <input name="{hid}-p{i}-chislo" size="6" placeholder="{esc(str(chis.get('ocr') or chis.get('visual') or ''))}"></label>
 </div>
</div>"""


def household_block(h: dict) -> str:
    hid = h["id"]
    crops = "".join(
        f'<img src="{esc(c)}" alt="{esc(c)}">' for c in h.get("crops", []))
    persons = "".join(person_block(hid, i, p)
                      for i, p in enumerate(h.get("persons", [])))
    refs = h.get("source_refs", {})
    unc = "".join(f"<li>{esc(u)}</li>" for u in h.get("uncertainties", []))
    return f"""
<section class="household" id="{hid}" data-hid="{hid}">
 <h2>{esc(hid)} — деревня Волченокъ · {esc(refs.get('page_id') or '')}</h2>
 <div class="meta">
  <div><b>Архив:</b> ЦГА Москвы ф. 597 оп. 1 д. 258 · снимок {esc(refs.get('page_id') or '')}
   · строки OCR: {esc(str(refs.get('lines')))}</div>
  <div><b>Номер двора (OCR-версия):</b> {esc(json.dumps(h.get('ledger_number', {}).get('ocr'), ensure_ascii=False))}</div>
 </div>
 <div class="crops">{crops}</div>
 {persons}
 <ul class="unc"><b>Открытые неопределённости (для человека):</b>{unc}</ul>
 <label class="done"><input type="checkbox" name="{hid}-human_verified">
  проверено человеком (двор прочитан целиком на снимке)</label>
 <label class="notes">заметки проверяющего
  <textarea name="{hid}-notes" rows="2"></textarea></label>
</section>"""


def main() -> int:
    data = json.loads(DATA.read_text())
    meta = data["meta"]
    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Волченокъ — выверка дворов (д. 258)</title>
<style>
 body{{font-family:Georgia,serif;max-width:1200px;margin:1em auto;padding:0 1em;color:#222}}
 .banner{{background:#fff3cd;border:1px solid #d4b106;padding:.6em;margin-bottom:1em}}
 .household{{border:1px solid #999;border-radius:6px;padding:1em;margin:2em 0}}
 h2{{margin-top:0}}
 .crops img{{width:100%;border:1px solid #ccc;margin:.3em 0}}
 .person{{border-top:1px dashed #bbb;padding:.5em 0}}
 .labels .role{{font-weight:bold;margin-right:1em}}
 .tag{{font-size:.75em;padding:.1em .4em;border-radius:3px;margin-right:.5em}}
 .tag.ocr{{background:#e8e8ff}} .tag.vis{{background:#ffe8e8}}
 .src .s{{margin:.15em 0 .15em 1em}}
 .s.hum{{background:#e8ffe8}} .s.warn{{color:#a00}}
 .inputs label{{display:block;margin:.25em 0 .25em 1em}}
 .inputs input{{margin-left:.5em}}
 .unc{{color:#555}}
 .done{{display:block;margin-top:.8em}} .notes{{display:block;margin-top:.4em}}
 .notes textarea{{width:100%}}
 #save{{position:fixed;right:1em;top:1em;background:#2a2;padding:.4em;border:0;color:#fff;cursor:pointer}}
</style></head><body>
<button id="save">Экспорт verified JSON</button>
<div class="banner">
 <b>Ничто здесь не считается проверенным, пока человек не заполнил поля и
 не поставил галочку «проверено человеком».</b> OCR и VISUAL-строки — только
 подсказки; историческая транскрипция двора 258A-H01 не менялась под OCR
 (расхождение «Демидов» / «-митрев-» зафиксировано как открытое).
 {esc(meta['section']['archive'])}
</div>
{''.join(household_block(h) for h in data['households'])}
<script>
const KEY='volchenok-household-review';
function collect(){{
  const out={{exported_at:new Date().toISOString(),human_verified_by:'(fill your name)',households:[]}};
  document.querySelectorAll('section.household').forEach(s=>{{
    const hid=s.dataset.hid;const e={{id:hid,persons:[],human_verified:s.querySelector('[name$="-human_verified"]').checked,notes:s.querySelector('[name$="-notes"]').value}};
    s.querySelectorAll('.person').forEach(p=>{{
      const i=p.dataset.person;
      const t=s.querySelector(`[name="${{hid}}-p${{i}}-verified_text"]`);
      if(t.value.trim()||s.querySelector(`[name="${{hid}}-p${{i}}-age"]`).value.trim())
        e.persons.push({{index:i,verified_text:t.value,age:s.querySelector(`[name="${{hid}}-p${{i}}-age"]`).value,chislo:s.querySelector(`[name="${{hid}}-p${{i}}-chislo"]`).value}});
    }});
    if(e.human_verified||e.persons.length||e.notes)out.households.push(e);
  }});
  return out;
}}
function persist(){{
  const st={{}};document.querySelectorAll('input,textarea').forEach(el=>{{st[el.name]=el.type==='checkbox'?el.checked:el.value;}});
  localStorage.setItem(KEY,JSON.stringify(st));
}}
function restore(){{
  const st=JSON.parse(localStorage.getItem(KEY)||'{{}}');
  document.querySelectorAll('input,textarea').forEach(el=>{{if(el.name in st){{if(el.type==='checkbox')el.checked=st[el.name];else el.value=st[el.name];}}}});
}}
document.addEventListener('input',persist);document.addEventListener('change',persist);
document.getElementById('save').onclick=()=>{{
  const blob=new Blob([JSON.stringify(collect(),null,1)],{{type:'application/json'}});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='volchenok-households-verified.json';a.click();
}};
restore();
</script></body></html>"""
    OUT.write_text(html)
    print(f"written -> {OUT} ({len(html)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
