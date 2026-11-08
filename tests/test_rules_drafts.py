from __future__ import annotations

import pytest

from triage_kit import rules
from triage_kit.drafts import check_draft
from triage_kit.models import Category, Priority


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("Please delete all my personal data", "data_protection_request"),
        ("I exercise my right to be forgotten", "data_protection_request"),
        ("Can I get an export of my data?", "data_protection_request"),
        ("What personal data do you hold about me?", "data_protection_request"),
        ("My lawyer will be in touch", "legal_threat"),
        ("I will take you to court", "legal_threat"),
        ("I filed a chargeback", "chargeback"),
        ("I will dispute the charge with my bank", "chargeback"),
        ("it smelled like burning plastic", "safety"),
        ("there were sparks from the socket", "safety"),
    ],
)
def test_rules_fire(text: str, rule: str) -> None:
    assert rule in rules.evaluate(text).hits


@pytest.mark.parametrize(
    "text",
    [
        "Is screen burn-in covered by the warranty?",
        "My data plan is too small",
        "We met at the food court",
        "Please charge my new card",
    ],
)
def test_near_misses_do_not_fire(text: str) -> None:
    assert rules.evaluate(text).hits == ()


def test_privacy_office_beats_escalations_and_floors_combine() -> None:
    o = rules.evaluate("Delete my data under the GDPR or my lawyer sues you. Sparks everywhere.")
    assert o.route == "privacy-office"
    assert o.force_category is Category.PRIVACY
    assert o.priority_floor is Priority.URGENT


def test_route_for_urgent_technical_goes_on_call() -> None:
    none = rules.evaluate("nothing special")
    assert rules.route_for(Category.TECHNICAL, Priority.URGENT, none) == "tech-oncall"
    assert rules.route_for(Category.TECHNICAL, Priority.HIGH, none) == "tech-support"


CLEAN = "Hello [NAME_1],\n\nThanks for your message. We are checking it.\n\nSupport team"


def test_clean_draft_passes() -> None:
    assert check_draft(CLEAN, {"[NAME_1]": "Ana"}) == []


@pytest.mark.parametrize(
    ("draft", "finding"),
    [
        ("We will refund the full amount.", "promises a refund"),
        ("Your refund has been issued.", "says a refund was issued"),
        ("As a goodwill gesture, here is a voucher.", "offers compensation"),
        ("We guarantee it will not happen again.", "guarantees"),
        ("You will hear from us within 24 hours.", "promises a deadline"),
        ("Your data has been deleted.", "confirms data deletion"),
        ("As an AI, I cannot say.", "mentions being an AI"),
        ("I passed this to tech-oncall.", "names internal queues"),
        ("Write to help@example.com instead.", "contains personal data"),
        ("We emailed [EMAIL_2].", "unknown placeholders"),
        ("", "empty draft"),
        ("x" * 1300, "too long"),
    ],
)
def test_draft_findings(draft: str, finding: str) -> None:
    findings = check_draft(draft, {"[EMAIL_1]": "a@example.com"})
    assert any(f.startswith(finding) for f in findings), findings
