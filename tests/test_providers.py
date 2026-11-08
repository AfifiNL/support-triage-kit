from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import ScriptedModel, out
from triage_kit.providers.base import ProviderError, TriageRequest
from triage_kit.providers.gemini import GeminiTriageModel
from triage_kit.providers.replay import (
    Cassette,
    RecordingTriageModel,
    ReplayTriageModel,
    request_hash,
)

REQ = TriageRequest("t1", "email", "standard", "Subject", "Body [EMAIL_1]", ("[EMAIL_1]",))


class FakeModels:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls = 0

    def generate_content(self, **_: Any) -> Any:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def gemini(result: Any, max_calls: int = 5) -> tuple[GeminiTriageModel, FakeModels]:
    models = FakeModels(result)
    return GeminiTriageModel(
        "g", max_calls=max_calls, client=SimpleNamespace(models=models)
    ), models


GOOD = SimpleNamespace(text=out().model_dump_json())


def test_gemini_parses_structured_output() -> None:
    model, _ = gemini(GOOD)
    assert model.triage(REQ).summary == "s"


def test_gemini_budget_zero_makes_no_call() -> None:
    model, models = gemini(GOOD, max_calls=0)
    with pytest.raises(ProviderError, match="budget exhausted"):
        model.triage(REQ)
    assert models.calls == 0


def test_gemini_budget_is_a_hard_cap() -> None:
    model, models = gemini(GOOD, max_calls=2)
    model.triage(REQ)
    model.triage(REQ)
    with pytest.raises(ProviderError):
        model.triage(REQ)
    assert models.calls == 2


def test_gemini_errors_hide_vendor_messages() -> None:
    class ClientError(Exception):
        code = 403

    model, _ = gemini(ClientError("secret-token-123 rejected"))
    with pytest.raises(ProviderError) as info:
        model.triage(REQ)
    assert "secret-token-123" not in str(info.value)
    assert "HTTP 403" in str(info.value)


def test_gemini_schema_mismatch() -> None:
    model, _ = gemini(SimpleNamespace(text='{"category": "nope"}'))
    with pytest.raises(ProviderError, match="schema"):
        model.triage(REQ)


def test_record_then_replay(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    live = ScriptedModel(out())
    rec = RecordingTriageModel(live, path)
    rec.triage(REQ)
    rec.cassette.save()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["meta"]["model"] == "scripted"
    assert rows[1]["request_hash"] == request_hash(REQ)

    replay = ReplayTriageModel(Cassette.load(path))
    assert replay.triage(REQ) == out()
    other = TriageRequest("t2", "email", "standard", "Other", "Body", ())
    with pytest.raises(ProviderError, match="re-record"):
        replay.triage(other)
    assert replay.cassette.misses == ["t2"]

    # Re-recording reuses the stored call instead of paying for it again.
    live2 = ScriptedModel(ProviderError("should not be called"))
    live2.name = "scripted"
    rec2 = RecordingTriageModel(live2, path)
    assert rec2.triage(REQ) == out()
    assert rec2.reused == 1
    assert live2.requests == []


def test_missing_cassette() -> None:
    with pytest.raises(FileNotFoundError):
        Cassette.load(Path("does/not/exist.jsonl"))
