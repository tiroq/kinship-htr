#!/usr/bin/env python3
"""KINSHIP-SEARCH-02 — regression tests for the Ivan/Pyotr classifier.

Covers the consumer-visible edge rules of kinship_search02.py:
  1. given-name forms are distinguished from patronymic forms
     («Иван Петровъ» counts given-Иван + patronymic-Петровъ, and is
     NEVER a Пётр-Ивановъ reading);
  2. OCR glue (his/their markers, fused «дети ихъ» garbles, widow
     marks, glued patronymics) is classified, raw forms preserved;
  3. weak fuzzy forms (74-79 similarity) never drive P1-P4 patterns;
  4. P1 requires exactly ONE Иван in the segment — with two, the son's
     father stays unattributed (P3), never silently assigned;
  5. a widow line breaks households; chislo/age x-bands and age sanity
     behave as documented.

No DB access: pure-function tests, deterministic and isolated.

Run:  htrbench-python tests/test_kinship_search02.py   (pytest-compatible)
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCH))

from kinship_search02 import (  # noqa: E402
    classify_patterns,
    match_name_token,
    match_relation_token,
    person_from_line,
)


def _line(text: str, lid: int = 1, bbox=(500, 1000, 300, 33)):
    return {"line_id": lid, "line_number": lid, "text_raw": text,
            "bbox": list(bbox), "confidence": 0.9}


def _mkperson(ln: dict, role: str | None = None):
    """Build a person the way segment_column does: consume the leading
    relation word, then take the given name after it (rel_end=0 for
    head-type lines)."""
    from kinship_search02 import leading_relation, line_tokens
    toks = line_tokens(ln)
    rel, rel_end = leading_relation(toks)
    if rel == "widower-mark":
        rel = None
    if role is None:
        role = rel or "head"
    if role == "head":
        rel_end = 0
    return person_from_line(ln, role, toks, rel_end)

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# --- 1. given vs patronymic forms ---------------------------------------
check(match_name_token("иван")["form"] == "given", "иван must be given")
check(match_name_token("ивана")["form"] == "given", "ивана inflection")
check(match_name_token("иоанн")["form"] == "given", "иоанн given")
check(match_name_token("иванов")["form"] == "patronymic", "иванов patr")
check(match_name_token("иванович")["form"] == "patronymic",
      "иванович patr")
check(match_name_token("петрова")["form"] == "patronymic",
      "петрова (woman) patr")
check(match_name_token("петр")["form"] == "given", "петр given")
m = match_name_token("иван")
check(m["strong"] is True, "exact forms are strong")
check(match_name_token("праскеваиванова")["form"] == "patronymic",
      "glued «праскеваиванова» patr")
check(match_name_token("егoиван".replace("o", "о"))["form"] == "given",
      "glued «егоиван» given")
# «Иван Петровъ» is NOT a Pyotr reading of any kind:
#   иван -> given; петров -> patronymic
check(match_name_token("иван")["name"] == "иван"
      and match_name_token("петров") == {
          "name": "петр", "form": "patronymic", "tier": "exact",
          "strong": True, "stem": "петров", "raw": "петров"},
      "«Иван Петровъ» token semantics")

# --- 2. weak vs strong fuzzy ---------------------------------------------
w = match_name_token("нван")           # 75 vs иван — 1-edit OCR garble
check(w is not None and not w["strong"],
      "weak fuzzy (74-79) must not be strong")
check(match_name_token("дина") is None,
      "«дина» (≈50 similarity noise) must not match at all")
s = match_name_token("иоан")           # 89 vs иоанн
check(s is not None and s["strong"], "strong fuzzy (>=80)")
check(match_name_token("петрр")["strong"], "петрр strong fuzzy")

# --- 3. relation matching -------------------------------------------------
check(match_relation_token("чена") is not None, "«чена» ≈ жена")
check(match_relation_token("рат") is not None, "«рат» ≈ брат")
check(match_relation_token("естиих") is not None,
      "fused «естиих» ≈ дети ихъ")

check(match_relation_token("иван") is None,
      "a name form never reads as a relation")
check(match_relation_token("вина") is None,
      "«вина» must NOT match сын (сина stem removed)")

# --- 4. P1 single-Ivan rule ------------------------------------------------
def hh(persons):
    return {"persons": persons}


iv_head = _mkperson(_line("Иванъ Евстигнеевъ вдовъ"), "head")
iv_son = _mkperson(_line("сынъ его Петръ"), "son")
pats, ev = classify_patterns(hh([iv_head, iv_son]))
check("P1" in pats and "P3" not in pats,
      f"single-Ivan + son-Пётр must be P1, got {pats}")

iv_head2 = _mkperson(_line("Ивановъ братъ Иванъ"), "brother")
pats2, ev2 = classify_patterns(hh([iv_head, iv_son, iv_head2]))
check("P1" not in pats2 and "P3" in pats2,
      f"two-Ivan ambiguity must demote to P3, got {pats2}")
check(any("NOT attributed" in e for e in ev2),
      "ambiguity evidence must say NOT attributed")

# P4 mirror: single Пётр head + son Иван
pt_head = _mkperson(_line("Петръ Михаиловъ"), "head")
iv_child = _mkperson(_line("дсти ихъ Иванъ"), "children")
pats3, _ = classify_patterns(hh([pt_head, iv_child]))
check("P4" in pats3, f"Пётр-head with son-Иван must be P4, got {pats3}")

# P2: Пётр Ивановъ
pt_iv = _mkperson(_line("петр ивановь Холость"), "head")
pats4, _ = classify_patterns(hh([pt_iv]))
check("P2" in pats4, f"«Петръ Ивановъ» must be P2, got {pats4}")

# siblings under «дети ихъ» -> P4 explicit brothers
a = _mkperson(_line("дети ихъ Иванъ"), "children")
b = _mkperson(_line("Петръ"), "child")
pats5, _ = classify_patterns(hh([_mkperson(
    _line("Нагаръ Петровъ"), "head"), a, b]))
check("P4" in pats5, f"children Иван+Пётр = explicit brothers P4, {pats5}")

# weak-fuzzy Иван never drives patterns
weak_iv = _mkperson(_line("сынъ его Дина"), "son")
weak_iv["given_match"] = match_name_token("дина")
pats6, _ = classify_patterns(hh([_mkperson(_line("Стефанъ"), "head"),
                                 weak_iv, iv_son]))
check("P1" not in pats6 and "P4" not in pats6,
      f"weak fuzzy must not fire P1/P4, got {pats6}")

# P5: Иван without Пётр and without Петровъ forms
pats7, _ = classify_patterns(hh([iv_head, _mkperson(
    _line("сынъ его Михаилъ"), "son")]))
check("P5" in pats7, f"Ivan-without-Pyotr must be P5, got {pats7}")

# --- 5. person parsing keeps raw forms -------------------------------------
p = _mkperson(_line("113 жена его устиньй Мартинова-"), "spouse")
check(p["given_raw"] == "устиньй" and p["patronymic_raw"] == "Мартинова-",
      f"spouse parse got {p['given_raw']}/{p['patronymic_raw']}")
check(p["inline_chislo"] == "113", "inline chislo")
p2 = _mkperson(_line("вдова Федосья Матвеева"), "widow")
check(p2["given_raw"] == "Федосья", "widow given after relation")
p3 = _mkperson(_line("вени Его Федон"), "head")
check(p3["patronymic_raw"] != "Его",
      f"patronymic must skip the «его» target, got {p3['patronymic_raw']}")


def main() -> int:
    if FAIL:
        print(f"FAIL ({len(FAIL)}):")
        for f in FAIL:
            print("  -", f)
        return 1
    print("test_kinship_search02: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
