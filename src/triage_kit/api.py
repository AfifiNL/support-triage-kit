"""HTTP API: a signed webhook for new tickets, and review endpoints for people.

Fail-closed: without TRIAGE_WEBHOOK_SECRET the webhook refuses every request, and without
TRIAGE_REVIEW_TOKEN the review endpoints do. There is no endpoint that sends a reply; approved
replies wait in the outbox for the helpdesk integration (for example the n8n workflow in
`workflows/`) to deliver and acknowledge.
"""

from __future__ import annotations

import hmac
import os
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from triage_kit import signing
from triage_kit.models import Ticket
from triage_kit.pipeline import redact_ticket, triage
from triage_kit.providers.base import TriageModel
from triage_kit.store import ReviewError, Store

MAX_BODY_BYTES = 64_000


class ApproveIn(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)
    reply: str | None = Field(default=None, max_length=4_000)


class RejectIn(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)
    reason: str = Field(default="", max_length=1_000)


def create_app(
    store: Store,
    model: TriageModel,
    *,
    webhook_secret: str | None,
    review_token: str | None,
) -> FastAPI:
    app = FastAPI(title="support-triage-kit", version="0.1.0")

    def reviewer_auth(authorization: Annotated[str | None, Header()] = None) -> None:
        if not review_token:
            raise HTTPException(503, "review API disabled: TRIAGE_REVIEW_TOKEN is not set")
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), review_token.encode()):
            raise HTTPException(401, "invalid reviewer token")

    Auth = Depends(reviewer_auth)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "model": model.name}

    @app.post("/v1/webhooks/tickets")
    async def ticket_webhook(request: Request) -> dict[str, Any]:
        if not webhook_secret:
            raise HTTPException(503, "webhook disabled: TRIAGE_WEBHOOK_SECRET is not set")
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, "ticket too large")
        try:
            signing.verify(body, request.headers.get(signing.HEADER), webhook_secret)
        except signing.SignatureError as exc:
            raise HTTPException(401, str(exc)) from None
        try:
            ticket = Ticket.model_validate_json(body)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors(include_input=False)) from None
        result = triage(ticket, model)
        red, _, _ = redact_ticket(ticket)
        rid, created = store.add(result, red.mapping)
        return {
            "review_id": rid,
            "duplicate": not created,
            "route": result.route,
            "priority": result.priority.value,
            "status": result.status,
        }

    @app.get("/v1/reviews", dependencies=[Auth])
    def list_reviews(status: str | None = None) -> list[dict[str, Any]]:
        return store.reviews(status)

    @app.get("/v1/reviews/{rid}", dependencies=[Auth])
    def get_review(rid: int) -> dict[str, Any]:
        try:
            view = store.view(rid)
        except ReviewError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        view["events"] = store.events(rid)
        return view

    @app.post("/v1/reviews/{rid}/approve", dependencies=[Auth])
    def approve(rid: int, body: ApproveIn) -> dict[str, Any]:
        try:
            return store.approve(rid, body.reviewer, body.reply)
        except ReviewError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @app.post("/v1/reviews/{rid}/reject", dependencies=[Auth])
    def reject(rid: int, body: RejectIn) -> dict[str, Any]:
        try:
            return store.reject(rid, body.reviewer, body.reason)
        except ReviewError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @app.get("/v1/outbox", dependencies=[Auth])
    def outbox() -> list[dict[str, Any]]:
        return store.outbox()

    @app.post("/v1/outbox/{oid}/ack", dependencies=[Auth])
    def ack(oid: int) -> dict[str, str]:
        try:
            store.ack(oid)
        except ReviewError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        return {"status": "acked"}

    return app


def app_from_env() -> FastAPI:  # pragma: no cover - wiring only
    from triage_kit.factory import build_model

    return create_app(
        Store(os.environ.get("TRIAGE_DB", "triage.sqlite3")),
        build_model(),
        webhook_secret=os.environ.get("TRIAGE_WEBHOOK_SECRET") or None,
        review_token=os.environ.get("TRIAGE_REVIEW_TOKEN") or None,
    )
