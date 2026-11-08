"""Deterministic redaction of personal data before any text reaches a model.

Each match is replaced by a typed placeholder (`[EMAIL_1]`, `[PHONE_2]`, ...). The same value
always gets the same placeholder within one ticket, so a model can still say "we sent the link
to [EMAIL_1]". The mapping stays in this process (and the review store); `restore` puts the
values back only into a reply a person has approved.

Detected: e-mail addresses, IBANs (mod-97 checked), payment card numbers (Luhn checked), IPv4
addresses, phone numbers (9+ digits), and names in two narrow patterns ("my name is X Y", and a
name on its own line after a sign-off such as "Kind regards,"). Postal addresses and names in
free text are NOT detected; see the README's limits.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b")
CARD = re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])")
IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
PHONE = re.compile(
    r"(?<![\w+])(?:\+\d{1,3}[ .-]?)?(?:\(\d{1,4}\)[ .-]?)?\d{2,5}(?:[ .-]?\d{2,5}){1,4}(?![\w])"
)
_NAME = r"[A-Z][A-Za-z'\u2019-]+(?: [A-Z][A-Za-z'\u2019-]+){0,2}"
NAME_INTRO = re.compile(rf"\b[Mm]y name is ({_NAME})")
SIGN_OFF = re.compile(
    r"^(?:Kind regards|Best regards|Regards|Best|Thanks|Thank you|Many thanks|Cheers|Sincerely)"
    rf"[,!.]?[ \t]*\n[ \t]*({_NAME})[ \t]*$",
    re.MULTILINE,
)
PLACEHOLDER = re.compile(r"\[(EMAIL|IBAN|CARD|IP|PHONE|NAME)_(\d+)\]")
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
    digits = re.sub(r"\D", "", value)
    return 13 <= len(digits) <= 19 and _luhn_ok(digits)


def _phone_ok(value: str) -> bool:
    # E.164 numbers have at most 15 digits; longer runs are not phone numbers.
    return 9 <= len(re.sub(r"\D", "", value)) <= 15


def _name_ok(value: str) -> bool:
    return value.split()[0] not in NOT_NAMES


def redact(text: str) -> Redaction:
    r = _Redactor()
    out = r.sub(text, EMAIL, "EMAIL")
    out = r.sub(out, IBAN, "IBAN", _iban_ok)
    out = r.sub(out, CARD, "CARD", _card_ok)
    out = r.sub(out, IPV4, "IP")
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


def restore(text: str, mapping: dict[str, str]) -> str:
    """Put original values back. Unknown placeholders are left as they are."""
    return PLACEHOLDER.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)
