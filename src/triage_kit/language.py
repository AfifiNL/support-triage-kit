"""English or Dutch, decided by counting common function words. Small, deterministic and
explainable; it picks the reply language (all rule patterns run whatever the language). Not a
general language identifier: anything else, and very short or evenly mixed text, falls back
to English."""

from __future__ import annotations

import re
from typing import Literal

Language = Literal["en", "nl"]

_WORD = re.compile(r"[^\W\d_]+")
# Words that exist in both languages ("is", "we", "of", "me", "in", "op"...) are left out of
# both lists, so they cannot tip a Dutch text towards English or the other way round.
_EN_A = "the a an i not and my that on to for with are at you please can have was it this"
_EN_B = "but your our be do does did will would could should thanks hello forgot broken"
_NL_A = "de het een ik niet en van mijn dat te voor met zijn bij wij u jullie graag kunt heb"
_NL_B = "dit maar uw onze mij ben doe wil zou kan hebben al nog ook geen wordt om aub bedankt"
_NL_C = "alstublieft alsjeblieft vergeten werkt kapot geeft doet hallo"
EN = frozenset(f"{_EN_A} {_EN_B}".split())
NL = frozenset(f"{_NL_A} {_NL_B} {_NL_C}".split())


def scores(text: str) -> tuple[int, int]:
    """(English, Dutch) function-word counts."""
    words = [w.lower() for w in _WORD.findall(text)]
    return sum(w in EN for w in words), sum(w in NL for w in words)


def detect(text: str) -> Language:
    """Dutch when Dutch words outnumber English ones; English otherwise (also on a tie)."""
    en, nl = scores(text)
    return "nl" if nl > en else "en"


def clearly_not(text: str, language: Language, margin: int = 2) -> bool:
    """True only when `text` is clearly in the other language (by `margin` words or more), so a
    short or mixed reply is never blocked for its language."""
    en, nl = scores(text)
    return (en - nl >= margin) if language == "nl" else (nl - en >= margin)
