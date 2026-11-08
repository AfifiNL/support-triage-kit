"""HTTP API: a signed webhook for new tickets, and review endpoints for people.

Three secrets, three roles, all fail-closed (unset = every request refused):
  TRIAGE_WEBHOOK_SECRET  signs incoming tickets (HMAC, see signing.py)
  TRIAGE_REVIEW_TOKEN    people: list, approve, reject, erase
  TRIAGE_OUTBOX_TOKEN    the delivery integration: read the outbox and acknowledge; it cannot
                         approve anything
There is no endpoint that sends a reply; approved replies wait in the outbox for the helpdesk
integration (for example the n8n workflow in `workflows/`) to deliver and acknowledge.
The reviewer name is recorded as given; put real user authentication in front before real use.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

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


class EraseIn(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)


def body_hash(ticket: Ticket) -> str:
    canonical = "\x1f".join([ticket.subject, ticket.body, ticket.channel, ticket.customer_tier])
    return hashlib.sha256(canonical.encode()).hexdigest()


def _bearer(expected: str | None, name: str) -> Callable[[str | None], None]:
    def check(authorization: Annotated[str | None, Header()] = None) -> None:
        if not expected:
            raise HTTPException(503, f"disabled: {name} is not set")
        scheme, _, given = (authorization or "").partition(" ")
        if scheme != "Bearer" or not hmac.compare_digest(given.encode(), expected.encode()):
            raise HTTPException(401, "invalid token")

    return check


def create_app(
    store: Store,
    model: TriageModel,
    *,
    webhook_secret: str | None,
    review_token: str | None,
    outbox_token: str | None = None,
) -> FastAPI:
    app = FastAPI(title="support-triage-kit", version="0.1.0")
    Auth = Depends(_bearer(review_token, "TRIAGE_REVIEW_TOKEN"))
    OutboxAuth = Depends(_bearer(outbox_token, "TRIAGE_OUTBOX_TOKEN"))

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
            raise HTTPException(
                422, exc.errors(include_input=False, include_context=False, include_url=False)
            ) from None
        digest = body_hash(ticket)
        known = store.find(ticket.id)
        if known is not None:
            rid, stored = known
            if stored != digest:
                raise HTTPException(409, "this ticket id was already received with other content")
            return {"review_id": rid, "duplicate": True}
        # The model call blocks; run it off the event loop so other requests are served.
        result = await run_in_threadpool(triage, ticket, model)
        red, _, _ = redact_ticket(ticket)
        rid, created = store.add(result, red.mapping, digest)
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

    @app.post("/v1/reviews/{rid}/erase", dependencies=[Auth])
    def erase(rid: int, body: EraseIn) -> dict[str, str]:
        try:
            store.forget(rid, body.reviewer)
        except ReviewError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        return {"status": "erased"}

    @app.get("/v1/outbox", dependencies=[OutboxAuth])
    def outbox() -> list[dict[str, Any]]:
        return store.outbox()

    @app.post("/v1/outbox/{oid}/ack", dependencies=[OutboxAuth])
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
        outbox_token=os.environ.get("TRIAGE_OUTBOX_TOKEN") or None,
    )
