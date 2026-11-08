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
# Name tokens: Unicode letters, apostrophes and hyphens; capitalisation is checked in `_name_ok`.
_NAME_PART = r"[^\W\d_][^\W\d_'\u2019-]*(?:['\u2019-][^\W\d_]+)*"
_NAME = rf"{_NAME_PART}(?: {_NAME_PART}){{0,2}}"
NAME_INTRO = re.compile(rf"\b(?i:my name is) ({_NAME})")
SIGN_OFF = re.compile(
    r"^(?i:kind regards|best regards|warm regards|regards|best|thanks|thank you|many thanks"
    r"|cheers|sincerely)"
    rf"[,!.]?[ \t]*\r?\n[ \t]*({_NAME})[ \t]*\r?$",
    re.MULTILINE,
)
PLACEHOLDER = re.compile(r"\[(EMAIL|IBAN|CARD|IP|PHONE|NAME)_(\d+)\]")
RESTORABLE = frozenset({"EMAIL", "PHONE", "NAME"})
# Capitalised words that can sit alone on the line after a sign-off but are not names.
NOT_NAMES = {"Order", "Account", "Customer", "Support", "Sent", "Ticket", "Reference"}


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


def _name_ok(value: str) -> bool:
    parts = value.split()
    return parts[0] not in NOT_NAMES and all(p[0].isupper() for p in parts)


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
    out = r.sub(out, NAME_INTRO, "NAME", _name_ok, group=1)
    out = r.sub(out, SIGN_OFF, "NAME", _name_ok, group=1)
    # A name found once is redacted everywhere it appears in the ticket.
    for ph, value in list(r.mapping.items()):
        if ph.startswith("[NAME_"):
            out = re.sub(rf"\b{re.escape(value)}\b", ph, out)
    return Redaction(out, r.mapping)


def placeholders_in(text: str) -> set[str]:
    return {m.group(0) for m in PLACEHOLDER.finditer(text)}


def restore(text: str, mapping: dict[str, str], kinds: frozenset[str] = RESTORABLE) -> str:
    """Put original values back for the given kinds (by default only e-mail addresses, phone
    numbers and names). Other and unknown placeholders are left as they are."""

    def repl(m: re.Match[str]) -> str:
        return mapping.get(m.group(0), m.group(0)) if m.group(1) in kinds else m.group(0)

    return PLACEHOLDER.sub(repl, text)
