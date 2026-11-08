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
        ("Please delete my account and all my data.", "data_protection_request"),
        ("What information do you have about me?", "data_protection_request"),
        ("I will sue you", "legal_threat"),
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
        "my colleague Sue said hello",
        "the smoke detector keeps beeping",
        "how do I burn a CD with your app",
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
        ("We'll send you a refund.", "promises a refund"),
        ("Your refund has been approved.", "says a refund was issued"),
        ("We have deleted your data as requested.", "confirms data deletion"),
        ("It arrives within 3-5 business days.", "promises a deadline"),
        ("Here is a 20% discount.", "offers compensation"),
        ("We have credited your account.", "offers compensation"),
        ("We see the charge on [CARD_1].", "repeats a card number"),
        ("We emailed [EMAIL_2].", "unknown placeholders"),
        ("", "empty draft"),
        ("x" * 1300, "too long"),
    ],
)
def test_draft_findings(draft: str, finding: str) -> None:
    findings = check_draft(draft, {"[EMAIL_1]": "a@example.com", "[CARD_1]": "4111"})
    assert any(f.startswith(finding) for f in findings), findings


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (
            "Op grond van de AVG verzoek ik u al mijn persoonsgegevens te verwijderen.",
            "data_protection_request",
        ),
        ("Welke persoonsgegevens hebben jullie van mij?", "data_protection_request"),
        ("Ik maak gebruik van mijn recht op vergetelheid.", "data_protection_request"),
        ("Ik heb mijn advocaat ingeschakeld.", "legal_threat"),
        ("Ik heb bij mijn bank een terugboeking aangevraagd.", "chargeback"),
        ("Ik rook een brandlucht bij de oplader.", "safety"),
    ],
)
def test_dutch_rules_fire(text: str, rule: str) -> None:
    assert rule in rules.evaluate(text).hits


@pytest.mark.parametrize(
    "text",
    ["Graag het bedrag terugboeken.", "the avg price went up", "Mijn rookmelder piept."],
)
def test_dutch_near_misses(text: str) -> None:
    assert rules.evaluate(text).hits == ()


@pytest.mark.parametrize(
    ("draft", "finding"),
    [
        ("Wij betalen het bedrag zo snel mogelijk terug.", "promises a refund"),
        ("Uw terugbetaling is verwerkt.", "says a refund was issued"),
        ("U krijgt 10% korting op uw volgende bestelling.", "offers compensation"),
        ("U hoort binnen 2 werkdagen van ons.", "promises a deadline"),
        ("Uw gegevens zijn verwijderd.", "confirms data deletion"),
    ],
)
def test_dutch_draft_findings(draft: str, finding: str) -> None:
    assert finding in check_draft(draft, {}, "nl")


def test_reply_must_be_in_the_tickets_language() -> None:
    english = "Hello,\n\nThanks for your message. We are looking into it.\n\nSupport team"
    dutch = "Hallo,\n\nBedankt voor uw bericht. We kijken ernaar.\n\nKlantenservice"
    assert check_draft(english, {}, "en") == []
    assert check_draft(dutch, {}, "nl") == []
    assert any("language" in f for f in check_draft(english, {}, "nl"))


@pytest.mark.parametrize(
    "draft",
    [
        "We hebben uw melding over de aangevraagde terugboeking ontvangen.",
        "We hebben uw verzoek om terugbetaling ontvangen en bekijken het.",
    ],
)
def test_dutch_nouns_for_the_customers_request_are_not_promises(draft: str) -> None:
    assert "promises a refund" not in check_draft(draft, {}, "nl")
