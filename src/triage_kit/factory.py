"""Builds the configured triage model from environment variables (all optional).

TRIAGE_PROVIDER         mock (default) | gemini | replay
TRIAGE_GEMINI_MODEL     default gemini-3.6-flash
TRIAGE_GEMINI_VERTEX    true to use Vertex AI (GOOGLE_CLOUD_PROJECT + ADC)
TRIAGE_GEMINI_LOCATION  Vertex location, e.g. eu
TRIAGE_GEMINI_THINKING_LEVEL  minimal | low | medium | high
TRIAGE_MAX_LIVE_CALLS   hard cap on live calls per process (default 0: no live calls)
TRIAGE_CASSETTE         replay: cassette to read
TRIAGE_RECORD_CASSETTE  gemini: also record every call to this cassette
"""

from __future__ import annotations

import os
from pathlib import Path

from triage_kit.providers.base import TriageModel
from triage_kit.providers.mock import MockTriageModel
from triage_kit.providers.replay import Cassette, RecordingTriageModel, ReplayTriageModel

DEFAULT_CASSETTE = "evals/fixtures/live-triage.jsonl"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def build_model() -> TriageModel:
    provider = _env("TRIAGE_PROVIDER", "mock")
    if provider == "mock":
        return MockTriageModel()
    if provider == "replay":
        return ReplayTriageModel(Cassette.load(Path(_env("TRIAGE_CASSETTE", DEFAULT_CASSETTE))))
    if provider == "gemini":
        from triage_kit.providers.gemini import GeminiTriageModel

        live: TriageModel = GeminiTriageModel(
            _env("TRIAGE_GEMINI_MODEL", "gemini-3.6-flash"),
            max_calls=int(_env("TRIAGE_MAX_LIVE_CALLS", "0") or 0),
            vertex=_env("TRIAGE_GEMINI_VERTEX").lower() in {"1", "true", "yes"},
            location=_env("TRIAGE_GEMINI_LOCATION") or None,
            thinking_level=_env("TRIAGE_GEMINI_THINKING_LEVEL") or None,
        )
        record = _env("TRIAGE_RECORD_CASSETTE")
        return RecordingTriageModel(live, Path(record)) if record else live
    raise ValueError(f"unknown TRIAGE_PROVIDER {provider!r} (mock | gemini | replay)")
