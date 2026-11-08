"""Checks on a model-written reply draft. A failed check never deletes the draft: the ticket
still goes to a person, flagged, with the findings, and the draft cannot be approved unedited.

Policy encoded here (generic, adjust to your own):
  * a draft may not promise money, compensation, deadlines or completed data deletion; those
    are decisions a person makes;
  * it may only use placeholders that exist in this ticket's redaction map;
  * it may not contain personal data of its own (the model never saw any, so any it writes is
    made up);
  * it may not reveal internal queue names or talk about being an AI model.
"""

from __future__ import annotations

import re

from triage_kit.redact import placeholders_in, redact
from triage_kit.rules import ESCALATIONS_QUEUE, ONCALL_QUEUE, ROUTES

MAX_CHARS = 1_200

COMMITMENTS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rx, re.IGNORECASE))
    for name, rx in (
        (
            "promises a refund",
            r"\b(we will|we'll|we have|we've|I will|I'll|I have) "
            r"(\w+ ){0,2}refund",
        ),
        ("says a refund was issued", r"\brefund (has been|was|is being) (issued|processed|sent)"),
        ("offers compensation", r"\b(compensat\w*|voucher|credit note|goodwill)"),
        ("guarantees", r"\bguarantee"),
        ("promises a deadline", r"\bwithin \d+ (minutes?|hours?|days?|business days?)\b"),
        (
            "confirms data deletion",
            r"\b(data|account|information) (has been|was|is now) "
            r"(deleted|erased|removed)",
        ),
        ("gives legal advice", r"\blegal advice\b"),
        ("mentions being an AI", r"\b(as an ai|language model|I am an ai|I'm an ai)\b"),
    )
)
INTERNAL_NAMES = {*ROUTES.values(), ESCALATIONS_QUEUE, ONCALL_QUEUE}


def check_draft(draft: str, mapping: dict[str, str]) -> list[str]:
    findings: list[str] = []
    text = draft.strip()
    if not text:
        return ["empty draft"]
    if len(text) > MAX_CHARS:
        findings.append(f"too long ({len(text)} > {MAX_CHARS} characters)")
    unknown = sorted(placeholders_in(text) - set(mapping))
    if unknown:
        findings.append(f"unknown placeholders: {', '.join(unknown)}")
    own_pii = redact(text).counts()
    if own_pii:
        kinds = ", ".join(f"{k} x{v}" for k, v in own_pii.items())
        findings.append(f"contains personal data the model was never given: {kinds}")
    for name, rx in COMMITMENTS:
        if rx.search(text):
            findings.append(name)
    low = text.lower()
    leaked = sorted(q for q in INTERNAL_NAMES if re.search(rf"\b{re.escape(q)}\b", low))
    # Team names that are plain English words ("billing") are fine in a reply; only flag the
    # hyphenated internal queue ids.
    leaked = [q for q in leaked if "-" in q]
    if leaked:
        findings.append(f"names internal queues: {', '.join(leaked)}")
    return findings
