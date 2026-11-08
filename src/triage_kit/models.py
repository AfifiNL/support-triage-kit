"""Data shapes shared by the pipeline, the providers, the API and the evals."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Category(StrEnum):
    BILLING = "billing"
    TECHNICAL = "technical"
    ACCOUNT = "account"
    SHIPPING = "shipping"
    CANCELLATION = "cancellation"
    PRIVACY = "privacy"
    FEEDBACK = "feedback"
    OTHER = "other"


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


PRIORITY_RANK = {Priority.LOW: 0, Priority.NORMAL: 1, Priority.HIGH: 2, Priority.URGENT: 3}


class Ticket(BaseModel):
    """An inbound support ticket as the helpdesk sends it (raw, may contain personal data)."""

    id: str = Field(min_length=1, max_length=128)
    subject: str = Field(default="", max_length=500)
    body: str = Field(min_length=1, max_length=20_000)
    channel: Literal["email", "chat", "form"] = "email"
    customer_tier: Literal["standard", "business"] = "standard"

    @field_validator("body")
    @classmethod
    def _has_text(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body is empty")
        return v


class ModelOutput(BaseModel):
    """What a triage model must return. The model only ever sees redacted text."""

    category: Category
    priority: Priority
    summary: str = Field(max_length=300)
    reply: str = Field(max_length=2_000)


class TriageResult(BaseModel):
    ticket_id: str
    category: Category
    priority: Priority
    route: str
    escalations: list[str]
    summary: str
    redacted_text: str
    pii_counts: dict[str, int]
    draft: str | None
    draft_findings: list[str]
    status: Literal["ready_for_review", "draft_blocked", "model_error"]
    model: str
    model_category: Category | None = None
    model_priority: Priority | None = None
    error: str | None = None
