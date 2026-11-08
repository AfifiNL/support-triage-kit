"""English or Dutch, decided by counting common function words. Small, deterministic and
explainable; good enough to pick the reply language and the rule patterns for support
tickets, not a general language identifier. Anything else falls back to English."""

from __future__ import annotations

import re
from typing import Literal

Language = Literal["en", "nl"]

_WORD = re.compile(r"[^\W\d_]+")
_EN_A = "the a an i not and of my is that on to for with are at we you please can have was"
_EN_B = "it this but your our me be do does did will would could should"
_NL_A = "de het een ik niet en van mijn dat op te voor met zijn bij wij u jullie graag kunt"
_NL_B = "heb was dit maar uw onze mij ben doe wil zou kan hebben al nog ook geen wordt om"
EN = frozenset(f"{_EN_A} {_EN_B}".split())
NL = frozenset(f"{_NL_A} {_NL_B}".split())


def detect(text: str) -> Language:
    words = [w.lower() for w in _WORD.findall(text)]
    en = sum(w in EN for w in words)
    nl = sum(w in NL for w in words)
    return "nl" if nl > en else "en"
