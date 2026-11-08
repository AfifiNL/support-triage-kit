"""redact → rules → model → merge (rules win) → route → draft checks → a person reviews.

The model fills one step. Everything around it is deterministic, so the safety properties
(no personal data to the model, statutory requests reach the privacy office, no unreviewed
promises) hold whatever the model returns, including when it fails.
"""

from __future__ import annotations

from triage_kit import rules
from triage_kit.drafts import check_draft
from triage_kit.models import Category, Priority, Ticket, TriageResult
from triage_kit.providers.base import ProviderError, TriageModel, TriageRequest
from triage_kit.redact import Redaction, redact

SEPARATOR = "\n\n-----\n\n"


def redact_ticket(ticket: Ticket) -> tuple[Redaction, str, str]:
    """Redact subject and body together so one value gets one placeholder across both."""
    both = redact(f"{ticket.subject}{SEPARATOR}{ticket.body}")
    subject, _, body = both.text.partition(SEPARATOR)
    return both, subject, body


def triage(ticket: Ticket, model: TriageModel) -> TriageResult:
    red, subject, body = redact_ticket(ticket)
    outcome = rules.evaluate(f"{subject}\n{body}")
    request = TriageRequest(
        ticket_id=ticket.id,
        channel=ticket.channel,
        customer_tier=ticket.customer_tier,
        subject=subject,
        body=body,
        placeholders=tuple(red.mapping),
    )
    try:
        out = model.triage(request)
    except Exception as exc:  # any failure must still reach a person
        # No model answer: the ticket still reaches a person, in the queue the rules pick,
        # or the general queue.
        category = outcome.force_category or Category.OTHER
        priority = rules.max_priority(Priority.NORMAL, outcome.priority_floor)
        return TriageResult(
            ticket_id=ticket.id,
            category=category,
            priority=priority,
            route=rules.route_for(category, priority, outcome),
            escalations=list(outcome.hits),
            summary="",
            redacted_text=red.text,
            pii_counts=red.counts(),
            draft=None,
            draft_findings=["no draft: the model call failed"],
            status="model_error",
            model=model.name,
            error=str(exc) if isinstance(exc, ProviderError) else type(exc).__name__,
        )
    category = outcome.force_category or out.category
    priority = rules.max_priority(out.priority, outcome.priority_floor)
    findings = check_draft(out.reply, red.mapping)
    return TriageResult(
        ticket_id=ticket.id,
        category=category,
        priority=priority,
        route=rules.route_for(category, priority, outcome),
        escalations=list(outcome.hits),
        summary=out.summary,
        redacted_text=red.text,
        pii_counts=red.counts(),
        draft=out.reply,
        draft_findings=findings,
        status="draft_blocked" if findings else "ready_for_review",
        model=model.name,
        model_category=out.category,
        model_priority=out.priority,
    )
