"""Stage D — search profiles for Волченки, Протасьево, Головкин.

Each profile separates:
  documented  — spellings attested in the case-repo documents (registers,
                transcripts, research state); these are evidence-linked;
  generated   — plausible inflections (marked generated) and systematic
                OCR-corruption variants (marked generated).  Generated
                variants widen recall only; a hit on a generated variant is
                a lead, never a spelling claim.

Corruption variants are produced mechanically by confusion substitutions
(1-2 edits) — the fuzzy layer additionally tolerates unseen corruptions.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

from .normalize import normalize

BENCH = Path("/Users/mysterx/dev/kinship-htr-bench")
PROFILE_PATH = BENCH / "work" / "index" / "search_profiles.json"

# Common Cyrillic OCR confusions for this model/handwriting (derived from
# bench-page misreadings; each pair is confused in either direction).
CONFUSIONS = {
    "о": "аудс", "а": "ое", "е": "осз", "с": "оеч", "д": "уъл",
    "у": "д", "н": "ипм", "и": "нплм", "п": "ин", "к": "нх",
    "л": "дп", "м": "шин", "ш": "мщ", "щ": "шч", "ч": "цс",
    "р": "пн", "т": "гр", "г": "тх", "з": "эсе", "ц": "ч",
    "в": "б", "б": "в", "ф": "т", "х": "кг", "ы": "ьм",
}


def corrupt(stem: str, max_variants: int = 120) -> list[str]:
    """All 1-edit substitutions plus selected 2-edit ones, capped."""
    one = []
    for i, ch in enumerate(stem):
        for rep in CONFUSIONS.get(ch, ""):
            one.append(stem[:i] + rep + stem[i + 1:])
    two: list[str] = []
    pairs = list(itertools.combinations(range(len(stem)), 2))
    for i, j in pairs[:400]:
        si = CONFUSIONS.get(stem[i], "")
        sj = CONFUSIONS.get(stem[j], "")
        if si and sj:
            for a in si[:2]:
                for b in sj[:2]:
                    v = stem[:i] + a + stem[i + 1:j] + b + stem[j + 1:]
                    two.append(v)
    out = []
    for v in dict.fromkeys(one + two):
        if v != stem:
            out.append(v)
        if len(out) >= max_variants:
            break
    return out


PROFILES = {
    "Волченки": {
        "type": "settlement",
        "canonical": "Волченки",
        "documented": [
            "Волченки", "Волченок", "Волченокъ", "Волченках",
            "Волченкам",
        ],
        "documented_note": (
            "Волченки (modern) and Волченокъ/Волченок (historical) attested in "
            "case-repo registers/transcripts (transcripts-2014/3592/2001/4436, "
            "research-state.md); Волченках/Волченкам are case inflections seen "
            "in context"),
        "inflections_generated": [
            "волченка", "волченку", "волченком", "волченковъ",
            "волченками", "волченке",
        ],
        "corruption_stems": ["волченки", "волченок", "волченокъ"],
    },
    "Протасьево": {
        "type": "settlement",
        "canonical": "Протасьево",
        "documented": [
            "Протасьево", "Протасьева", "Протасьевъ",
            "Николаевская ц. Протасьева",
        ],
        "documented_note": (
            "Протасьево/Протасьева attested in artifact/claims registers and "
            "transcripts-4635b etc.; church 'Николаевская ц. Протасьева' per "
            "research-state.md"),
        "inflections_generated": [
            "протасьеву", "протасьеве", "протасьев", "протасьевой",
        ],
        "corruption_stems": ["протасьево", "протасьева"],
    },
    "Головкин": {
        "type": "surname",
        "canonical": "Головкин",
        "documented": [
            "Головкин", "Головкина", "Головкинъ", "Головкиных",
            "Головкиной",
        ],
        "documented_note": (
            "attested in person/claims/artifact registers and final-report.md; "
            "владение графини Е. А. Головкиной per research-state.md"),
        "inflections_generated": [
            "головкину", "головкиным", "головкине", "головкинам",
        ],
        "corruption_stems": ["головкин", "головкина"],
    },
    "Егоров": {
        "type": "name",
        "canonical": "Егоров",
        "documented": ["Егоров", "Егоровъ", "Егорова", "Егорьев"],
        "documented_note": (
            "historical spelling forms (pre-reform orthography preserved for "
            "display); attestation NOT yet linked to case-repo documents — "
            "treated as search spellings only. Form can be a patronymic "
            "(from Егоръ) or a hereditary surname; household context decides."),
        "inflections_generated": [
            "егорову", "егоровой", "егоровых", "егорьеву", "егорьевой",
        ],
        "corruption_stems": ["егоров", "егорова", "егорьев"],
    },
    "Царев": {
        "type": "name",
        "canonical": "Царев",
        "documented": ["Царев", "Царевъ", "Царёв", "Царева"],
        "documented_note": (
            "historical spelling forms (ё preserved in display, folded for "
            "retrieval); attestation NOT yet linked to case-repo documents. "
            "Likely patronymic from Царь/Царёк or surname; context decides."),
        "inflections_generated": ["цареву", "царевой", "царевых", "царевам"],
        "corruption_stems": ["царев", "царева"],
    },
    "Демидов": {
        "type": "name",
        "canonical": "Демидов",
        "documented": ["Демидов", "Демидовъ", "Демидова"],
        "documented_note": (
            "human-read attestation: scan 258-p014 (case repo) — «Той же "
            "вотчины деревни Волченокъ крестьянин Никита Демидовъ, жена его "
            "Устинья Мартынова». The OCR of that line is corrupted beyond "
            "spelling-variant recall («умитревъ»), so the household is also "
            "retrieved through its documented co-names (see "
            "household_anchors_documented). Patronymic from Демидъ or "
            "surname; household context decides."),
        "inflections_generated": ["демидову", "демидовой", "демидовых"],
        "corruption_stems": ["демидов", "демидова"],
        "household_anchors_documented": {
            "source": (
                "human transcript of scan 258-p014 (kinship case file); "
                "surname itself is NOT OCR-readable on that scan"),
            "co_tokens": ["никита", "устинья", "мартынова"],
            "note": (
                "Leads retrieved via these co-names are HOUSEHOLD-ANCHOR "
                "leads: the matched lines contain no Демидов form in the "
                "OCR text and the link to the surname rests on the human "
                "transcript only — never treat as a verified surname match."),
        },
    },
    "Мефодиев": {
        "type": "name",
        "canonical": "Мефодиев",
        "documented": ["Мефодиев", "Мефодьев", "Мефодий"],
        "documented_note": (
            "calendar name Мефодий with patronymic forms Мефодиев/Мефодьев "
            "(both spellings occur in pre-reform sources); attestation NOT "
            "yet linked to case-repo documents."),
        "inflections_generated": ["мефодиева", "мефодьеву", "мефодьева",
                                  "мефодиеву"],
        "corruption_stems": ["мефодиев", "мефодьев", "мефодий"],
    },
    "Иванов": {
        "type": "name",
        "canonical": "Иванов",
        "priority": "low — extremely common; review with case filter",
        "documented": ["Иванов", "Ивановъ", "Иванова"],
        "documented_note": (
            "historical spelling forms; mostly patronymics in this period. "
            "LOW PRIORITY: very common name, high false-positive load."),
        "inflections_generated": ["иванову", "ивановой", "ивановых"],
        "corruption_stems": ["иванов", "иванова"],
    },
    "Петров": {
        "type": "name",
        "canonical": "Петров",
        "priority": "low — extremely common; review with case filter",
        "documented": ["Петров", "Петровъ", "Петрова"],
        "documented_note": (
            "historical spelling forms; mostly patronymics in this period. "
            "LOW PRIORITY: very common name, high false-positive load."),
        "inflections_generated": ["петрову", "петровой", "петровых"],
        "corruption_stems": ["петров", "петрова"],
    },
    "Симеонов": {
        "type": "name",
        "canonical": "Симеонов",
        "documented": ["Симеонов", "Симеоновъ", "Симеонова", "Семенов",
                       "Семеновъ", "Семенова"],
        "documented_note": (
            "calendar name Симеонъ with patronymic Симеонов; the OCR and "
            "clerks conflate Симеонов/Семенов, so both are searched; "
            "attestation NOT yet linked to case-repo documents."),
        "inflections_generated": ["симеонову", "симеоновой", "семенову",
                                  "семеновой"],
        "corruption_stems": ["симеонов", "семенов", "симеонова", "семенова"],
    },
    "Шамрай": {
        "type": "surname",
        "canonical": "Шамрай",
        "documented": ["Шамрай", "Шамрая", "Шамраю", "Шамраев", "Шамраевъ",
                       "Шамраева", "Шамраевой"],
        "documented_note": (
            "ГАВО ф.И-331 оп.3 д.611 (Христорождественская ц. слободы "
            "Михайловки, Богучарский уезд, 1907) search pass "
            "(KINSHIP-SHAMRAI-04). Historical spelling uncertain: the "
            "surname is family-reported (P-0009/P-0178, ЦАМО cards) and "
            "Шамраевъ/Шамрай both plausible pre-reform clerk forms; all "
            "are search spellings, none attested in this book yet."),
        "inflections_generated": ["шамраем", "шамраев", "шамраеву",
                                  "шамраевых", "шамраевым"],
        "corruption_stems": ["шамрай", "шамраев", "шамрая"],
    },
    "Ефим": {
        "type": "name",
        "canonical": "Ефим",
        "documented": ["Ефим", "Ефима", "Ефиму", "Евфимий", "Евфимия",
                       "Ефимий", "Ефимъ", "Евфимію"],
        "documented_note": (
            "calendar baptismal name for P-0009 (family-reported birth "
            "01.04.1907; Julian/Gregorian reading open, see SHAMRAI-03 "
            "H4). Church form Евфимій, clerk/family form Ефимъ; both "
            "searched. Attested as a SEARCH NAME only — no birth entry "
            "verified yet."),
        "inflections_generated": ["ефимом", "евфимием", "ефимиею",
                                  "евфимию"],
        "corruption_stems": ["ефим", "евфим", "ефимий"],
    },
}


def profile_variants(name: str) -> dict:
    """Return {"documented": [...], "generated": [...], "all": [...]}
    with normalized (lowercase, historic-mapped) strings."""
    p = PROFILES[name]
    documented = sorted({normalize(s) for s in p["documented"] if normalize(s)})
    generated = sorted({normalize(s) for s in p["inflections_generated"]
                        if normalize(s)})
    for stem in p["corruption_stems"]:
        n = normalize(stem)
        if n:
            generated.extend(v for v in corrupt(n) if v not in documented)
    generated = sorted(set(generated) - set(documented))
    return {"documented": documented, "generated": generated,
            "all": documented + generated}


def export_profiles(path: Path = PROFILE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, p in PROFILES.items():
        v = profile_variants(name)
        out[name] = {
            "type": p["type"], "canonical": p["canonical"],
            "documented_spellings": p["documented"],
            "documented_note": p["documented_note"],
            "documented_normalized": v["documented"],
            "generated_inflections": p["inflections_generated"],
            "generated_corruptions_count": len(v["generated"]),
            "generated_corruptions_sample": v["generated"][:40],
        }
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"profiles written -> {path}")


def load_profiles() -> dict:
    return PROFILES


if __name__ == "__main__":
    export_profiles()
