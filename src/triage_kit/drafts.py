"""Checks on a model-written reply draft. A failed check never deletes the draft: the ticket
still goes to a person, flagged, with the findings, and the draft cannot be approved unedited.

Policy encoded here (generic, adjust to your own):
  * a draft may not promise money, compensation, deadlines or completed data deletion; those
    are decisions a person makes. The patterns over-flag on purpose ("we cannot promise a
    refund" is flagged too): a flag only means a person rewrites the sentence;
  * it may only use placeholders that exist in this ticket's redaction map, and never those
    for card numbers, IBANs or IP addresses (they are never restored into a reply);
  * it may not contain personal data of its own (the model never saw any, so any it writes is
    made up);
  * it may not reveal internal queue names or talk about being an AI model;
  * it must be in the ticket's language (English or Dutch).
The commitment patterns exist in English and Dutch.
"""

from __future__ import annotations

import re

from triage_kit.language import clearly_not
from triage_kit.redact import RESTORABLE, placeholders_in, redact
from triage_kit.rules import ESCALATIONS_QUEUE, ONCALL_QUEUE, ROUTES

MAX_CHARS = 1_200

COMMITMENTS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rx, re.IGNORECASE))
    for name, rx in (
        ("promises a refund", r"\b(we|I)\b[^.!?\n]{0,40}\brefund"),
        (
            "says a refund was issued",
            r"\brefund\b[^.!?\n]{0,30}\b(approved|issued|processed|sent|on its way|initiated"
            r"|started)",
        ),
        (
            "offers compensation",
            r"\b(compensat\w*|vouchers?|credit note|goodwill|discount\w*|\d+ ?% off"
            r"|free (months?|shipping|upgrade)|credited)",
        ),
        ("guarantees", r"\bguarantee"),
        (
            "promises a deadline",
            r"\bwithin (\d+( ?[-\u2013] ?\d+)?|an?|one|two|three) (minutes?|hours?|days?|weeks?"
            r"|business days?|working days?)\b|\bby (today|tonight|tomorrow|end of (the )?day)\b",
        ),
        (
            "confirms data deletion",
            r"\b(data|account|information) (has been|was|is now) (deleted|erased|removed)"
            r"|\b(we|I)('ve| have| has)? (now )?(deleted|erased|removed)\b",
        ),
        ("gives legal advice", r"\blegal advice\b"),
        ("mentions being an AI", r"\b(as an ai|language model|I am an ai|I'm an ai)\b"),
        # Dutch
        (
            "promises a refund",
            # verbs only: "terugboeking"/"terugbetaling" as nouns name the customer's request
            r"\b(wij|we|ik)\b[^.!?\n]{0,40}\b(terugbetalen|terugbetaald|terugstorten"
            r"|teruggestort|terugboeken|teruggeboekt|geld terug)\b"
            r"|\b(u krijgt|je krijgt|krijgt u)\b[^.!?\n]{0,30}\b(geld|bedrag)\b"
            r"[^.!?\n]{0,15}\bterug\b"
            r"|\b(wordt|worden) (terugbetaald|teruggestort)\b|\brestitutie\b"
            # separable verb: "wij betalen het bedrag ... terug"
            r"|\b(wij|we|ik)\b[^.!?\n]{0,40}\b(betalen|storten|boeken)\b[^.!?\n]{0,40}\bterug\b",
        ),
        (
            "says a refund was issued",
            r"\bterugbetaling\b[^.!?\n]{0,30}\b(verwerkt|gestort|goedgekeurd|uitgevoerd"
            r"|onderweg|in gang gezet)",
        ),
        (
            "offers compensation",
            r"\b(compensatie|schadevergoeding|tegoedbon|waardebon|tegemoetkoming|korting\w*"
            r"|tegoed|gecrediteerd|vergoeden|vergoeding|gratis (maand|maanden|verzending))",
        ),
        ("guarantees", r"\bgarande\w*|\bgegarandeerd\b"),
        (
            "promises a deadline",
            r"\bbinnen (\d+( ?[-\u2013] ?\d+)?|een|\u00e9\u00e9n|twee|drie) (minuut|minuten|uur"
            r"|uren|dag|dagen|werkdag|werkdagen|week|weken)\b"
            r"|\b(vandaag nog|morgen|uiterlijk morgen|voor het einde van de dag)\b",
        ),
        (
            "confirms data deletion",
            r"\b(gegevens|account)\b[^.!?\n]{0,20}\b(zijn|is)\b[^.!?\n]{0,20}"
            r"\b(verwijderd|gewist|opgeheven)\b"
            r"|\b(wij|we|ik) (hebben|heb)\b[^.!?\n]{0,30}\b(verwijderd|gewist)\b",
        ),
        ("gives legal advice", r"\bjuridisch advies\b"),
        ("mentions being an AI", r"\b(als (een )?ai|taalmodel|ai[- ]assistent|chatbot)\b"),
    )
)
INTERNAL_NAMES = {*ROUTES.values(), ESCALATIONS_QUEUE, ONCALL_QUEUE}


def check_draft(draft: str, mapping: dict[str, str], language: str | None = None) -> list[str]:
    findings: list[str] = []
    text = draft.strip()
    if not text:
        return ["empty draft"]
    if len(text) > MAX_CHARS:
        findings.append(f"too long ({len(text)} > {MAX_CHARS} characters)")
    unknown = sorted(placeholders_in(text) - set(mapping))
    if unknown:
        findings.append(f"unknown placeholders: {', '.join(unknown)}")
    sensitive = sorted(p for p in placeholders_in(text) if p[1:].split("_")[0] not in RESTORABLE)
    if sensitive:
        findings.append(f"repeats a card number, IBAN or IP address: {', '.join(sensitive)}")
    own_pii = redact(text).counts()
    if own_pii:
        kinds = ", ".join(f"{k} x{v}" for k, v in own_pii.items())
        findings.append(f"contains personal data the model was never given: {kinds}")
    for name, rx in COMMITMENTS:
        if rx.search(text) and name not in findings:
            findings.append(name)
    if language == "en" and clearly_not(text, "en"):
        findings.append("reply is not in the ticket's language (en)")
    elif language == "nl" and clearly_not(text, "nl"):
        findings.append(f"reply is not in the ticket's language ({language})")
    low = text.lower()
    leaked = sorted(q for q in INTERNAL_NAMES if re.search(rf"\b{re.escape(q)}\b", low))
    # Team names that are plain English words ("billing") are fine in a reply; only flag the
    # hyphenated internal queue ids.
    leaked = [q for q in leaked if "-" in q]
    if leaked:
        findings.append(f"names internal queues: {', '.join(leaked)}")
    return findings
