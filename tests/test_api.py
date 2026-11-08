from __future__ import annotations

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from conftest import ScriptedModel, out
from triage_kit import signing
from triage_kit.api import create_app
from triage_kit.providers.mock import MockTriageModel
from triage_kit.store import Store

SECRET = "test-webhook"
TOKEN = "test-review"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def client(store: Store | None = None, model: object | None = None, **kw: str | None) -> TestClient:
    opts = {"webhook_secret": SECRET, "review_token": TOKEN, **kw}
    return TestClient(create_app(store or Store(), model or MockTriageModel(), **opts))  # type: ignore[arg-type]


def post(c: TestClient, ticket: dict[str, str], secret: str = SECRET, ts: int | None = None):  # type: ignore[no-untyped-def]
    body = json.dumps(ticket).encode()
    return c.post(
        "/v1/webhooks/tickets",
        content=body,
        headers={signing.HEADER: signing.sign(body, secret, ts)},
    )


T1 = {"id": "a1", "subject": "Locked out", "body": "I'm locked out. Mail: jo@example.com"}


def test_signed_ticket_is_triaged_and_queued() -> None:
    c = client()
    r = post(c, T1)
    assert r.status_code == 200
    assert r.json()["route"] == "accounts"
    reviews = c.get("/v1/reviews?status=pending", headers=AUTH).json()
    assert [v["ticket_id"] for v in reviews] == ["a1"]
    assert "jo@example.com" not in reviews[0]["triage"]["redacted_text"]
    assert "jo@example.com" in reviews[0]["draft_for_reviewer"]


@pytest.mark.parametrize(
    ("kind", "detail"),
    [
        ("missing", "missing signature header"),
        ("wrong-secret", "signature does not match"),
        ("stale", "outside the allowed window"),
        ("garbage", "malformed"),
    ],
)
def test_bad_signatures_are_rejected(kind: str, detail: str) -> None:
    c = client()
    body = json.dumps(T1).encode()
    headers = {
        "missing": {},
        "wrong-secret": {signing.HEADER: signing.sign(body, "other")},
        "stale": {signing.HEADER: signing.sign(body, SECRET, int(time.time()) - 3600)},
        "garbage": {signing.HEADER: "nonsense"},
    }[kind]
    r = c.post("/v1/webhooks/tickets", content=body, headers=headers)
    assert r.status_code == 401
    assert detail in r.json()["detail"]


def test_tampered_body_is_rejected() -> None:
    c = client()
    body = json.dumps(T1).encode()
    sig = signing.sign(body, SECRET)
    r = c.post(
        "/v1/webhooks/tickets", content=body.replace(b"a1", b"a2"), headers={signing.HEADER: sig}
    )
    assert r.status_code == 401


def test_fail_closed_without_configuration() -> None:
    c = client(webhook_secret=None, review_token=None)
    assert post(c, T1).status_code == 503
    assert c.get("/v1/reviews").status_code == 503


def test_reviewer_token_required() -> None:
    c = client()
    assert c.get("/v1/reviews").status_code == 401
    assert c.get("/v1/reviews", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_duplicate_delivery_is_idempotent() -> None:
    c = client()
    first, second = post(c, T1).json(), post(c, T1).json()
    assert first["review_id"] == second["review_id"]
    assert second["duplicate"] is True
    assert len(c.get("/v1/reviews", headers=AUTH).json()) == 1


def test_invalid_ticket_is_422() -> None:
    assert post(client(), {"id": "x"}).status_code == 422


def test_approve_flow_restores_personal_data_in_outbox() -> None:
    c = client()
    rid = post(c, T1).json()["review_id"]
    r = c.post(f"/v1/reviews/{rid}/approve", headers=AUTH, json={"reviewer": "amy"})
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    box = c.get("/v1/outbox", headers=AUTH).json()
    assert len(box) == 1
    assert "jo@example.com" in box[0]["reply"]
    assert c.post(f"/v1/outbox/{box[0]['id']}/ack", headers=AUTH).status_code == 200
    assert c.get("/v1/outbox", headers=AUTH).json() == []
    assert c.post("/v1/outbox/999/ack", headers=AUTH).status_code == 404
    again = c.post(f"/v1/reviews/{rid}/approve", headers=AUTH, json={"reviewer": "amy"})
    assert again.status_code == 409
    events = c.get(f"/v1/reviews/{rid}", headers=AUTH).json()["events"]
    assert [e["action"] for e in events] == ["triaged", "approved"]


def test_blocked_draft_needs_an_edited_reply() -> None:
    model = ScriptedModel(out(reply="We will refund you right away."))
    c = client(model=model)
    rid = post(c, T1).json()["review_id"]
    r = c.post(f"/v1/reviews/{rid}/approve", headers=AUTH, json={"reviewer": "amy"})
    assert r.status_code == 422
    r = c.post(
        f"/v1/reviews/{rid}/approve",
        headers=AUTH,
        json={"reviewer": "amy", "reply": "Hello, a refund is on its way to [EMAIL_1]."},
    )
    assert r.status_code == 200
    assert c.get("/v1/outbox", headers=AUTH).json()[0]["reply"].endswith("jo@example.com.")


def test_reject_sends_nothing() -> None:
    c = client()
    rid = post(c, T1).json()["review_id"]
    r = c.post(f"/v1/reviews/{rid}/reject", headers=AUTH, json={"reviewer": "amy"})
    assert r.json()["status"] == "rejected"
    assert c.get("/v1/outbox", headers=AUTH).json() == []
    assert c.get("/v1/reviews/999", headers=AUTH).status_code == 404


def test_event_log_is_append_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = Store(tmp_path / "t.sqlite3")
    c = client(store=store)
    post(c, T1)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._db.execute("UPDATE events SET actor = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._db.execute("DELETE FROM events")
