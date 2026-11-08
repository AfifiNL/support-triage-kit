from __future__ import annotations

from conftest import ScriptedModel, out
from triage_kit.models import Category, Priority, Ticket
from triage_kit.pipeline import triage
from triage_kit.providers.mock import MockTriageModel

GDPR = Ticket(
    id="t1",
    subject="Delete my data",
    body="Under the GDPR delete all my personal data. Email jo@example.com, +44 7700 900123."
    "\n\nRegards,\nJo Smith",
)


def test_model_never_sees_personal_data() -> None:
    model = ScriptedModel(out())
    triage(GDPR, model)
    sent = model.requests[0]
    for value in ("jo@example.com", "+44 7700 900123", "Jo Smith"):
        assert value not in sent.subject + sent.body
    assert set(sent.placeholders) == {"[EMAIL_1]", "[PHONE_1]", "[NAME_1]"}


def test_rules_override_the_model() -> None:
    model = ScriptedModel(out(Category.FEEDBACK, Priority.LOW))
    r = triage(GDPR, model)
    assert r.category is Category.PRIVACY
    assert r.priority is Priority.HIGH
    assert r.route == "privacy-office"
    assert r.model_category is Category.FEEDBACK  # kept for the record


def test_model_can_raise_priority_above_the_floor() -> None:
    r = triage(GDPR, ScriptedModel(out(Category.PRIVACY, Priority.URGENT)))
    assert r.priority is Priority.URGENT


def test_model_failure_still_reaches_a_person(failing_model: ScriptedModel) -> None:
    r = triage(GDPR, failing_model)
    assert r.status == "model_error"
    assert r.route == "privacy-office"
    assert r.draft is None
    assert r.error == "boom"


def test_model_inventing_contact_details_blocks_the_draft() -> None:
    bad = out(reply="Please call us on +44 7700 900999 or mail fake@example.org.")
    r = triage(Ticket(id="t2", body="Where is my parcel?"), ScriptedModel(bad))
    assert r.status == "draft_blocked"
    assert any("personal data" in f for f in r.draft_findings)


def test_mock_end_to_end() -> None:
    r = triage(
        Ticket(id="t3", subject="Outage", body="Outage: nobody can log in, all users affected"),
        MockTriageModel(),
    )
    assert r.priority is Priority.URGENT
    assert r.status == "ready_for_review"
