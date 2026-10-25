"""Text helpers shared by every quote, substring and similarity check (spec Section 5.0, `norm_ar`)."""

from __future__ import annotations

import re
import unicodedata

_DIACRITICS = re.compile("[ً-ْٰ]")
_TATWEEL = "ـ"
_ALEFS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه"})
_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_WESTERN_TO_ARABIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
_SPACES = re.compile(r"\s+")
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U00002b00-\U00002bff\U00002300-\U000023ff\U00002190-\U000021ff"
    "\U0000fe0e\U0000fe0f\U0000200d\U000020e3\U000e0020-\U000e007f]"
)


def _is_punct(ch: str) -> bool:
    return unicodedata.category(ch).startswith(("P", "S")) and ch not in "/"


def norm_ar(text: str) -> str:
    """Remove diacritics and tatweel, unify alef/ya/ta marbuta, Arabic-Indic digits to Western, lowercase Latin,
    strip punctuation, collapse whitespace. The fraction slash is kept so "1/4" stays one token."""
    t = unicodedata.normalize("NFC", text or "")
    t = _DIACRITICS.sub("", t).replace(_TATWEEL, "")
    t = t.translate(_ALEFS).translate(_ARABIC_INDIC).lower()
    t = "".join(" " if _is_punct(ch) else ch for ch in t)
    return _SPACES.sub(" ", t).strip()


def tokens(text: str) -> list[str]:
    """Display tokens: the text split on whitespace, punctuation kept on the token (it is what the caption shows)."""
    return [w for w in _SPACES.split((text or "").strip()) if w]


def strip_emoji(text: str) -> str:
    return _EMOJI.sub("", text or "")


def to_display_digits(text: str, mode: str) -> str:
    """`DISPLAY_DIGITS`: `western` keeps 0-9, `arabic_indic` shows ٠-٩ (captions and widget labels)."""
    return text.translate(_WESTERN_TO_ARABIC) if mode == "arabic_indic" else text


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Word error rate between two texts after `norm_ar` (Levenshtein distance on words / reference length)."""
    ref, hyp = norm_ar(reference).split(), norm_ar(hypothesis).split()
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / len(ref)
