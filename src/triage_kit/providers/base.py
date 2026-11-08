from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from triage_kit.models import ModelOutput


class ProviderError(RuntimeError):
    """A model call failed or returned something unusable. Never carries vendor messages."""


@dataclass(frozen=True)
class TriageRequest:
    ticket_id: str
    channel: str
    customer_tier: str
    subject: str  # redacted
    body: str  # redacted
    placeholders: tuple[str, ...]
    language: str = "en"


class TriageModel(Protocol):
    name: str

    def triage(self, request: TriageRequest) -> ModelOutput: ...
