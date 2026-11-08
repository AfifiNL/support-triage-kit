"""Record a live run once; replay it offline in CI at zero cost.

A cassette is JSONL: one `{"meta": ...}` line, then one line per call, keyed by a hash of
exactly what the model was sent (system prompt, user prompt, output schema). Replay is strict:
a request the cassette has not seen means the model's input changed, so it is a miss that fails
the eval (re-record with `make record-live`); it never falls back to a live call.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from triage_kit.models import ModelOutput
from triage_kit.providers.base import ProviderError, TriageModel, TriageRequest
from triage_kit.providers.prompts import OUTPUT_JSON_SCHEMA, SYSTEM_PROMPT, build_user_prompt

MISS_HINT = "no recorded response for this request; re-record the cassette (make record-live)"


def request_hash(request: TriageRequest) -> str:
    blob = json.dumps(
        {"system": SYSTEM_PROMPT, "user": build_user_prompt(request), "schema": OUTPUT_JSON_SCHEMA},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


class Cassette:
    def __init__(self, path: Path, meta: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self.meta: dict[str, Any] = dict(meta or {})
        self.entries: dict[str, dict[str, Any]] = {}
        self.misses: list[str] = []
        self.errors: list[str] = []
        self._lock = threading.Lock()

    @classmethod
    def load(cls, path: Path) -> Cassette:
        if not Path(path).exists():
            raise FileNotFoundError(f"cassette {path} does not exist; record it first")
        c = cls(path)
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if "meta" in row:
                c.meta = row["meta"]
            else:
                c.entries[row["request_hash"]] = row
        return c

    def save(self) -> None:
        rows = sorted(self.entries.values(), key=lambda e: (e["ticket_id"], e["request_hash"]))
        lines = [json.dumps({"meta": self.meta}, sort_keys=True, ensure_ascii=False)]
        lines += [json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class ReplayTriageModel:
    def __init__(self, cassette: Cassette) -> None:
        self.cassette = cassette
        self.name = f"replay:{cassette.meta.get('model', '?')}"

    def triage(self, request: TriageRequest) -> ModelOutput:
        entry = self.cassette.entries.get(request_hash(request))
        if entry is None:
            with self.cassette._lock:
                self.cassette.misses.append(request.ticket_id)
            raise ProviderError(MISS_HINT)
        return ModelOutput.model_validate(entry["response"])


class RecordingTriageModel:
    """Wraps a live model; stores every successful call. Reuses calls recorded earlier with
    the same model and the same request, so a re-recording pays only for what changed."""

    def __init__(self, live: TriageModel, path: Path) -> None:
        self.live = live
        self.name = live.name
        prior = Cassette.load(path) if Path(path).exists() else None
        stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.cassette = Cassette(path, {"model": live.name, "recorded_at": stamp})
        self._prior = prior.entries if prior and prior.meta.get("model") == live.name else {}
        self.reused = 0

    def triage(self, request: TriageRequest) -> ModelOutput:
        h = request_hash(request)
        if h in self._prior:
            with self.cassette._lock:
                self.cassette.entries[h] = self._prior[h]
                self.reused += 1
            return ModelOutput.model_validate(self._prior[h]["response"])
        try:
            out = self.live.triage(request)
        except ProviderError as exc:
            with self.cassette._lock:
                self.cassette.errors.append(f"{request.ticket_id}: {exc}")
            raise
        with self.cassette._lock:
            self.cassette.entries[h] = {
                "request_hash": h,
                "ticket_id": request.ticket_id,
                "response": out.model_dump(mode="json"),
            }
        return out
