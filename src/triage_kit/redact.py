"""Deterministic redaction of personal data before any text reaches a model.

Each match is replaced by a typed placeholder (`[EMAIL_1]`, `[PHONE_2]`, ...). The same value
always gets the same placeholder within one ticket, so a model can still say "we sent the link
to [EMAIL_1]". The mapping stays in this process (and the review store); `restore` puts the
values back only into a reply a person has approved.

Detected: e-mail addresses (including non-ASCII ones), IBANs (mod-97 checked, any case), payment
card numbers (Luhn checked, with a trailing security code), IPv4 and IPv6 addresses, phone
numbers (9 to 15 digits), and names in two narrow patterns ("my name is X Y", and a name on its
own line after a sign-off such as "Kind regards,"). Postal addresses and names in free text are
NOT detected; see the README's limits.

Restoring is selective: e-mail addresses, phone numbers and names may go back into a reply a
person approved; card numbers, IBANs and IP addresses never do (`RESTORABLE`).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass, field

EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,}")
IBAN = re.compile(r"\b[A-Za-z]{2}\d{2}(?: ?[A-Za-z0-9]){11,34}\b")
# A card number, optionally followed by a short group (security code or expiry digits).
CARD = re.compile(r"(?<![\w.-])\d(?:[ .-]?\d){12,22}(?![\w-])")
IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
PHONE = re.compile(
    r"(?<![\w+])(?:\+\d{1,3}[ .-]?)?(?:\(\d{1,4}\)[ .-]?)?\d{2,5}(?:[ .-]?\d{2,5}){1,4}(?![\w])"
)
# Name tokens: Unicode letters, apostrophes, hyphens, initials ("J."); capitals: `_strict_name`.
_NAME_PART = r"(?:[^\W\d_]\.|[^\W\d_][^\W\d_'\u2019-]*(?:['\u2019-][^\W\d_]+)*)"
# Dutch surnames carry lower-case particles: "Mila van den Berg", "Jan de Vries".
_PARTICLE = r"(?:van|de|der|den|het|ter|ten|te|op|in|'t)"
_NAME = rf"{_NAME_PART}(?: (?:{_PARTICLE} )*{_NAME_PART}){{0,2}}"
NAME_INTRO = re.compile(rf"\b(?i:my name is|mijn naam is) ({_NAME})")
SIGN_OFF = re.compile(
    r"^(?i:kind regards|best regards|warm regards|regards|best|thanks|thank you|many thanks"
    r"|cheers|sincerely"
    r"|met vriendelijke groet(?:en)?|vriendelijke groet(?:en)?|hartelijke groet(?:en)?"
    r"|groet(?:en|jes)?|mvg|bedankt|alvast bedankt)"
    r"[,!.]?[ \t]*\r?\n[ \t]*([^\r\n]+?)[ \t]*\r?$",
    re.MULTILINE,
)
PLACEHOLDER = re.compile(r"\[(EMAIL|IBAN|CARD|IP|PHONE|NAME)_(\d+)\]")
RESTORABLE = frozenset({"EMAIL", "PHONE", "NAME"})
# Capitalised words that can sit alone on the line after a sign-off but are not names.
NOT_NAMES = {
    "Order",
    "Account",
    "Customer",
    "Support",
    "Sent",
    "Ticket",
    "Reference",
    "Bestelling",
    "Klant",
    "Klantnummer",
    "Verzonden",
    "Klantenservice",
    "Klantenteam",
    "Team",
}


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _iban_ok(value: str) -> bool:
    s = value.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    num = "".join(str(int(c, 36)) for c in moved)
    return int(num) % 97 == 1


@dataclass
class Redaction:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)  # placeholder -> original value

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for ph in self.mapping:
            m = PLACEHOLDER.fullmatch(ph)
            if m:
                out[m.group(1)] = out.get(m.group(1), 0) + 1
        return dict(sorted(out.items()))


class _Redactor:
    def __init__(self) -> None:
        self.mapping: dict[str, str] = {}
        self._by_value: dict[tuple[str, str], str] = {}
        self._n: dict[str, int] = {}

    def placeholder(self, kind: str, value: str) -> str:
        key = (kind, value)
        if key not in self._by_value:
            self._n[kind] = self._n.get(kind, 0) + 1
            ph = f"[{kind}_{self._n[kind]}]"
            self._by_value[key] = ph
            self.mapping[ph] = value
        return self._by_value[key]

    def sub(
        self,
        text: str,
        rx: re.Pattern[str],
        kind: str,
        ok: Callable[[str], bool] = lambda _: True,
        group: int = 0,
    ) -> str:
        def repl(m: re.Match[str]) -> str:
            value = m.group(group)
            if not ok(value):
                return m.group(0)
            ph = self.placeholder(kind, value)
            if group == 0:
                return ph
            start, end = m.span(group)
            s0 = m.start(0)
            whole = m.group(0)
            return whole[: start - s0] + ph + whole[end - s0 :]

        return rx.sub(repl, text)


def _card_ok(value: str) -> bool:
    """A Luhn-valid number of 13 to 19 digits, possibly followed by a few more digits (a
    security code or expiry typed straight after it): the whole run is then redacted."""
    digits = re.sub(r"\D", "", value)
    return any(_luhn_ok(digits[:n]) for n in range(min(19, len(digits)), 12, -1))


def _ipv6_ok(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).version == 6
    except ValueError:
        return False


def _phone_ok(value: str) -> bool:
    # E.164 numbers have at most 15 digits; longer runs are not phone numbers.
    return 9 <= len(re.sub(r"\D", "", value)) <= 15


_PARTICLES = frozenset({"van", "de", "der", "den", "het", "ter", "ten", "te", "op", "in", "'t"})


_TITLES = frozenset({"mw.", "mevr.", "dhr.", "mr.", "mrs.", "ms.", "dr."})
# Words that can stand alone after a sign-off (or start a sentence) but are not names.
_NOT_NAME_WORDS = frozenset({"i", "ik", "we", "wij", "hij", "zij", "you", "u", "jij"})
_NOT_NAME_PARTS = ("service", "support", "team")


def _capitalised(token: str) -> bool:
    return token[0].isupper()


def _strict_name(value: str) -> bool:
    """A whole string that is a person's name: capitalised words (initials allowed), lower-case
    surname particles inside, an optional title in front, at least one word of 2+ letters."""
    parts = value.split()
    while parts and parts[0].lower() in _TITLES:
        parts = parts[1:]
    if not parts or len(parts) > 5:
        return False
    first = parts[0]
    if first.lower() in _PARTICLES or first.lower() in _NOT_NAME_WORDS or first in NOT_NAMES:
        return False
    if any(w in value.lower() for w in _NOT_NAME_PARTS):
        return False
    if not any(len(p) >= 2 and not p.endswith(".") for p in parts):
        return False
    return all(p in _PARTICLES or _capitalised(p) for p in parts) and parts[-1] not in _PARTICLES


def _trim_name(value: str) -> str | None:
    """The capitalised name at the start of `value`: stop at the first lower-case word that is
    not a surname particle, drop trailing particles. None when nothing name-like is left."""
    kept: list[str] = []
    for part in value.split():
        if (part in _PARTICLES and kept) or _capitalised(part):
            kept.append(part)
        else:
            break
    while kept and kept[-1] in _PARTICLES:
        kept.pop()
    name = " ".join(kept)
    return name if kept and _strict_name(name) else None


def _sub_intro(r: _Redactor, text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        name = _trim_name(m.group(1))
        if name is None:
            return m.group(0)
        start = m.start(1) - m.start(0)
        whole = m.group(0)
        return whole[:start] + r.placeholder("NAME", name) + whole[start + len(name) :]

    return NAME_INTRO.sub(repl, text)


def _sub_signoff(r: _Redactor, text: str) -> str:
    """The line after a sign-off is redacted only if ALL of it is one or more names
    ("Sanne", "Sanne en Pieter", "J. de Vries"); otherwise nothing on it is touched."""

    def repl(m: re.Match[str]) -> str:
        line = m.group(1)
        names = [n.strip() for n in re.split(r"\s+(?:en|and|&)\s+|\s*,\s*", line) if n.strip()]
        if not names or not all(_strict_name(n) for n in names):
            return m.group(0)
        out = line
        for n in names:
            out = out.replace(n, r.placeholder("NAME", n), 1)
        start = m.start(1) - m.start(0)
        whole = m.group(0)
        return whole[:start] + out + whole[start + len(line) :]

    return SIGN_OFF.sub(repl, text)


def _also_parts(value: str) -> list[str]:
    """Shorter forms of a full name that identify the person too: the surname with its
    particles ("van Dijk") and the first name ("Sanne"), when at least 3 letters long."""
    parts = [p for p in value.split() if p.lower() not in _TITLES]
    forms: list[str] = []
    if len(parts) >= 2:
        surname = parts[1:]
        while surname and surname[0] in _PARTICLES and len(surname) > 1:
            forms.append(" ".join(surname))
            surname = surname[1:]
        if len(surname[0]) >= 3 and not surname[0].endswith("."):
            forms.append(surname[0])
        if len(parts[0]) >= 3 and not parts[0].endswith("."):
            forms.append(parts[0])
    return forms


def _sub_iban(r: _Redactor, text: str) -> str:
    """The IBAN pattern can run into a following word ("... 32 EUR"); shorten the match one
    character at a time until the checksum holds, and keep the rest of the text."""

    def repl(m: re.Match[str]) -> str:
        whole = m.group(0)
        for end in range(len(whole), 14, -1):
            candidate = whole[:end].rstrip()
            if _iban_ok(candidate):
                return r.placeholder("IBAN", candidate) + whole[len(candidate) :]
        return whole

    return IBAN.sub(repl, text)


def redact(text: str) -> Redaction:
    r = _Redactor()
    out = r.sub(text, EMAIL, "EMAIL")
    out = _sub_iban(r, out)
    out = r.sub(out, CARD, "CARD", _card_ok)
    out = r.sub(out, IPV4, "IP")
    out = r.sub(out, IPV6, "IP", _ipv6_ok)
    out = r.sub(out, PHONE, "PHONE", _phone_ok)
    out = _sub_intro(r, out)
    out = _sub_signoff(r, out)
    # A name found once is redacted everywhere it appears in the ticket, also as surname
    # ("Van Dijk" / "van Dijk") or first name alone. Longest forms first.
    for ph, value in list(r.mapping.items()):
        if ph.startswith("[NAME_"):
            for form in [value, *_also_parts(value)]:
                rx = re.escape(form)
                first = form.split()[0]
                if first in _PARTICLES:  # a particle is capitalised at a sentence start
                    rx = f"(?i:{re.escape(first)}){re.escape(form[len(first) :])}"
                out = re.sub(rf"(?<![\w\[]){rx}(?![\w\]])", ph, out)
    return Redaction(out, r.mapping)


def placeholders_in(text: str) -> set[str]:
    return {m.group(0) for m in PLACEHOLDER.finditer(text)}


def restore(text: str, mapping: dict[str, str], kinds: frozenset[str] = RESTORABLE) -> str:
    """Put original values back for the given kinds (by default only e-mail addresses, phone
    numbers and names). Other and unknown placeholders are left as they are."""

    def repl(m: re.Match[str]) -> str:
        return mapping.get(m.group(0), m.group(0)) if m.group(1) in kinds else m.group(0)

    return PLACEHOLDER.sub(repl, text)
