"""Gemini adapter (optional extra: `support-triage-kit[gemini]`).

Authentication is left to the google-genai SDK and the environment: GEMINI_API_KEY for the
Gemini Developer API, or Vertex AI (TRIAGE_GEMINI_VERTEX=true) with GOOGLE_CLOUD_PROJECT and
Application Default Credentials. This module never reads, logs or returns credentials.

Spend guard: every instance has a hard call budget (TRIAGE_MAX_LIVE_CALLS, default 0). With the
default, a live model refuses to make any call at all, so a misconfigured test or eval cannot
spend money. Retries done by the SDK count as one call here; the budget caps requests issued.
"""

from __future__ import annotations

import threading
from typing import Any

from pydantic import ValidationError

from triage_kit.models import ModelOutput
from triage_kit.providers.base import ProviderError, TriageRequest
from triage_kit.providers.prompts import SYSTEM_PROMPT, build_user_prompt

RETRY_STATUS = (408, 429, 500, 502, 503, 504)


def _describe(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    return type(exc).__name__ + (f" (HTTP {code})" if code else "")


class CallBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def take(self) -> None:
        with self._lock:
            if self.used >= self.limit:
                raise ProviderError(
                    f"live call budget exhausted ({self.used}/{self.limit}); "
                    "raise TRIAGE_MAX_LIVE_CALLS deliberately to allow more"
                )
            self.used += 1


class GeminiTriageModel:
    def __init__(
        self,
        model: str,
        *,
        max_calls: int,
        vertex: bool = False,
        location: str | None = None,
        thinking_level: str | None = None,
        timeout_s: float = 60.0,
        client: Any = None,
    ) -> None:
        self.model = model
        self.name = f"gemini:{model}" + (
            f":thinking-{thinking_level.lower()}" if thinking_level else ""
        )
        self.thinking_level = thinking_level.upper() if thinking_level else None
        self.budget = CallBudget(max_calls)
        self._client = (
            client if client is not None else self._make_client(vertex, location, timeout_s)
        )

    @staticmethod
    def _make_client(vertex: bool, location: str | None, timeout_s: float) -> Any:
        from google import genai
        from google.genai import types

        http = types.HttpOptions(
            timeout=int(timeout_s * 1000),
            retry_options=types.HttpRetryOptions(
                attempts=5, initial_delay=2.0, max_delay=30.0, http_status_codes=list(RETRY_STATUS)
            ),
        )
        if vertex:
            return genai.Client(vertexai=True, location=location or None, http_options=http)
        return genai.Client(http_options=http)

    def triage(self, request: TriageRequest) -> ModelOutput:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=ModelOutput,
            thinking_config=(
                types.ThinkingConfig(thinking_level=types.ThinkingLevel(self.thinking_level))
                if self.thinking_level
                else None
            ),
        )
        self.budget.take()  # only after the request is fully built
        try:
            resp = self._client.models.generate_content(
                model=self.model, contents=build_user_prompt(request), config=config
            )
        except Exception as exc:
            raise ProviderError(f"Gemini call failed: {_describe(exc)}") from None
        try:
            return ModelOutput.model_validate_json(resp.text or "")
        except ValidationError:
            raise ProviderError("Gemini output did not match the triage schema") from None
