"""Deterministic rules that run before and after the model and always win over it.

Some tickets must reach a person in a specific team whatever a model thinks: data-protection
requests, legal threats, chargebacks and safety issues. These are matched on the redacted text
with plain patterns, so the behaviour is explainable and testable. The model can raise a
priority, never lower one of these floors, and cannot move these tickets to another queue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from triage_kit.models import PRIORITY_RANK, Category, Priority

ROUTES: dict[Category, str] = {
    Category.BILLING: "billing",
    Category.TECHNICAL: "tech-support",
    Category.ACCOUNT: "accounts",
    Category.SHIPPING: "logistics",
    Category.CANCELLATION: "retention",
    Category.PRIVACY: "privacy-office",
    Category.FEEDBACK: "customer-care",
    Category.OTHER: "customer-care",
}
ESCALATIONS_QUEUE = "escalations"
ONCALL_QUEUE = "tech-oncall"


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    priority_floor: Priority
    force_category: Category | None = None
    route: str | None = None


def _rx(*alternatives: str) -> re.Pattern[str]:
    return re.compile("|".join(alternatives), re.IGNORECASE)


RULES: tuple[Rule, ...] = (
    Rule(
        "data_protection_request",
        _rx(
            r"\b(delete|erase|remove)\b[^.!?\n]{0,40}\bmy (personal )?(data|information|details)\b",
            r"\bright to (be forgotten|erasure|access)\b",
            r"\bGDPR\b",
            r"\bdata subject\b",
            r"\b(copy|export) of (all )?(the )?(my )?(personal )?data\b",
            r"\bwhat (personal )?(data|information) do you (hold|have|keep|store)\b",
            r"\bsubject access request\b",
            # Dutch
            # "AVG" is the Dutch name of the GDPR; case-sensitive ("avg" is an average), and not
            # the antivirus product of the same name.
            r"(?-i:\bAVG\b)(?!\s+(anti|internet|secure|tuneup|driver))",
            r"\bavg[- ]?(verzoek|recht)",
            r"\b(verwijder|wis)\w*\b[^.!?\n]{0,40}\b(mijn )?(persoons)?(gegevens|data)\b",
            # Dutch puts the verb last: "mijn gegevens laten verwijderen"
            r"\b(mijn|al mijn) (persoons)?(gegevens|data)\b[^.!?\n]{0,40}\b(verwijder\w*|wissen"
            r"|inzien|inzage)\b",
            r"\bpersoonsgegevens\b[^.!?\n]{0,40}\b(verwijder\w*|wissen|inzage|inzien)\b",
            r"\bwelke (persoons)?gegevens\b[^.!?\n]{0,30}\b(hebben|bewaren|heeft|bewaart"
            r"|verwerken|verwerkt)\b",
            r"\binzage\b[^.!?\n]{0,30}\b(persoons)?gegevens\b",
            r"\b(kopie|overzicht|export) van (al )?mijn (persoons)?(gegevens|data)\b",
            r"\brecht op (vergetelheid|inzage|verwijdering|dataportabiliteit)\b",
            r"\binzageverzoek\b",
        ),
        Priority.HIGH,
        force_category=Category.PRIVACY,
        route=ROUTES[Category.PRIVACY],
    ),
    Rule(
        "legal_threat",
        _rx(
            r"\b(my )?(lawyer|attorney|solicitor)s?\b",
            r"\blegal action\b",
            r"\b(will|going to|gonna|'ll|to|and) sue\b",
            r"\bsuing\b",
            r"\bsmall claims\b",
            r"\btake (you|this) to court\b",
            r"\badvoca(at|ten)\b",
            r"\bjuridische? stappen\b",
            r"\brechtszaak\b",
            r"\bnaar de (rechter|rechtbank)\b",
            r"\baansprakelijk (stellen|gesteld|stel)\b|\bstel\w*\b[^.!?\n]{0,15}\baansprakelijk\b",
            r"\bingebrekestelling\b",
            r"\bgeschillencommissie\b",
            r"\bdagvaard\w*\b",
        ),
        Priority.HIGH,
        route=ESCALATIONS_QUEUE,
    ),
    Rule(
        "chargeback",
        _rx(
            r"\bchargeback\b",
            r"\bdispute (the|this|a) (charge|payment|transaction)\b",
            r"\b(reported|reporting) (it|this|the charge|you) to my bank\b",
            # "terugboeking" alone is also a plain refund; a chargeback goes through the bank
            r"\bbank\b[^.!?\n]{0,40}\bterug(boeking|boeken|draaien|halen)\b",
            r"\bterug(boeking|boeken|draaien|halen)\b[^.!?\n]{0,40}\b(via|bij) (mijn|de) bank\b",
            r"\b(storneer|storneren|gestorneerd|stornering)\b",
            r"\bbetwist\w*\b[^.!?\n]{0,20}\b(betaling|afschrijving|incasso)\b",
            r"\b(betaling|afschrijving) (laten )?terugdraaien\b",
        ),
        Priority.HIGH,
        route=ESCALATIONS_QUEUE,
    ),
    Rule(
        "safety",
        _rx(
            r"\b(smoke|smoking)\b(?! (detector|alarm)s?\b)",
            r"\b(caught fire|on fire|sparks?|overheat\w*|injur\w*|electric shock)\b",
            r"\bburn(ed|t|ing)?\b(?!-in)(?! (a |the |my |your )?(cds?|dvds?|discs?|disks?|iso)\b)",
            r"\b(brandlucht|in brand|vonken|vonkt|oververhit\w*|rookontwikkeling|brandwond\w*)\b",
            r"\b(elektrische schok|een schok)\b",
            # "rook" is also "smelled"; only smoke coming out of something counts
            r"\bbegon te roken\b|\b(er kwam|er komt|kwam|komt) rook\b|\brook (kwam|komt) uit\b",
            r"\b(vatte vlam|vlam gevat|ontplof\w*|gewond)\b",
        ),
        Priority.URGENT,
        route=ESCALATIONS_QUEUE,
    ),
)


@dataclass(frozen=True)
class RuleOutcome:
    hits: tuple[str, ...]
    priority_floor: Priority | None
    force_category: Category | None
    route: str | None


def evaluate(text: str) -> RuleOutcome:
    hits = [r for r in RULES if r.pattern.search(text)]
    floor: Priority | None = None
    for r in hits:
        if floor is None or PRIORITY_RANK[r.priority_floor] > PRIORITY_RANK[floor]:
            floor = r.priority_floor
    category = next((r.force_category for r in hits if r.force_category), None)
    # Escalations beat team queues; the privacy office beats escalations because a statutory
    # deadline applies to those requests.
    route = next((r.route for r in hits if r.force_category is Category.PRIVACY), None) or next(
        (r.route for r in hits if r.route), None
    )
    return RuleOutcome(tuple(r.name for r in hits), floor, category, route)


def max_priority(a: Priority, b: Priority | None) -> Priority:
    if b is None:
        return a
    return a if PRIORITY_RANK[a] >= PRIORITY_RANK[b] else b


def route_for(category: Category, priority: Priority, outcome: RuleOutcome) -> str:
    if outcome.route:
        return outcome.route
    if category is Category.TECHNICAL and priority is Priority.URGENT:
        return ONCALL_QUEUE
    return ROUTES[category]
