from __future__ import annotations

import pytest

from triage_kit.models import Category, ModelOutput, Priority
from triage_kit.providers.base import ProviderError, TriageRequest


class ScriptedModel:
    """A model that returns whatever a test tells it to, and remembers what it was sent."""

    name = "scripted"

    def __init__(self, output: ModelOutput | Exception) -> None:
        self.output = output
        self.requests: list[TriageRequest] = []

    def triage(self, request: TriageRequest) -> ModelOutput:
        self.requests.append(request)
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def out(
    category: Category = Category.OTHER,
    priority: Priority = Priority.NORMAL,
    reply: str = "Hello,\n\nThanks, we are looking into it.\n\nSupport team",
) -> ModelOutput:
    return ModelOutput(category=category, priority=priority, summary="s", reply=reply)


@pytest.fixture
def failing_model() -> ScriptedModel:
    return ScriptedModel(ProviderError("boom"))
