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
            r"\b(delete|erase|remove) (all )?(of )?my (personal )?(data|information|details)",
            r"\bright to (be forgotten|erasure|access)\b",
            r"\bGDPR\b",
            r"\bdata subject\b",
            r"\b(copy|export) of (all )?(the )?(my )?(personal )?data\b",
            r"\bwhat (personal )?data do you (hold|have|keep|store)\b",
            r"\bsubject access request\b",
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
            r"\b(sue|suing)\b",
            r"\bsmall claims\b",
            r"\btake (you|this) to court\b",
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
        ),
        Priority.HIGH,
        route=ESCALATIONS_QUEUE,
    ),
    Rule(
        "safety",
        _rx(
            r"\b(smoke|smoking|caught fire|on fire|sparks?|overheat\w*|injur\w*|electric shock)\b",
            r"\bburn(ed|t|ing)?\b(?!-in)",
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
