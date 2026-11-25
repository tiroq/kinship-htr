"""Retrieval-only normalization for historical Cyrillic OCR text.

NEVER written back over diplomatic OCR (lines.text_raw); only lines.text_norm
and query strings pass through here.

Maps:
- old Cyrillic letters (ѣ і ї ѵ ѳ ѥ) to modern equivalents;
- final/hard signs ъ ь -> dropped (retrieval only);
- ё -> е;
- Latin homoglyphs the CRNN often emits (a c e o p x y) -> Cyrillic;
- punctuation -> space, case -> lower.
"""
from __future__ import annotations

import re

_OLD_CYR = str.maketrans({
    "ѣ": "е", "Ѣ": "е", "і": "и", "І": "и", "ї": "и", "Ї": "и",
    "ѵ": "и", "Ѵ": "и", "ѳ": "ф", "Ѳ": "ф", "ѥ": "е", "Њ": "е",
    "ѐ": "е", "Ѐ": "е", "й": "и",   # й folded to и for recall on OCR й/и swaps
})
_LATIN_HOMOGLYPH = str.maketrans({
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
    "A": "а", "C": "с", "E": "е", "O": "о", "P": "р", "X": "х", "Y": "у",
})
_KEEP = re.compile(r"[^а-я0-9 ]+")


def normalize(s: str) -> str:
    """Historical-Cyrillic + homoglyph normalization for retrieval only."""
    if not s:
        return ""
    s = s.translate(_OLD_CYR).lower().replace("ё", "е")
    s = s.replace("ъ", "").replace("ь", "")
    s = s.translate(_LATIN_HOMOGLYPH)
    s = _KEEP.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def trigrams(s: str) -> list[str]:
    n = normalize(s)
    if len(n) < 3:
        return [n] if n else []
    return [n[i:i + 3] for i in range(len(n) - 2)]
