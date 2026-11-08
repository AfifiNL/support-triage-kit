"""Offline end-to-end demo: three signed tickets in through the webhook, a person approves one
(edited) and rejects one, the outbox shows what would be sent. In-process, no network, no keys."""

from __future__ import annotations

import json
import secrets

from fastapi.testclient import TestClient

from triage_kit import signing
from triage_kit.api import create_app
from triage_kit.providers.mock import MockTriageModel
from triage_kit.store import Store

TICKETS = [
    {
        "id": "demo-1",
        "subject": "Charged twice for my order",
        "body": "Hi, my card was charged twice for order A-1042. Please fix this.\n\n"
        "Kind regards,\nJordan Lee",
    },
    {
        "id": "demo-2",
        "subject": "Please delete my data",
        "body": "I want you to delete all my personal data under the GDPR. "
        "My e-mail is jordan.lee@example.com.",
    },
    {
        "id": "demo-3",
        "subject": "Locked out",
        "body": "I'm locked out of my account and the reset mail never arrives at "
        "sam.rivera@example.org. Call me on +44 7700 900123.",
    },
]


def run_demo() -> int:
    webhook_secret = secrets.token_hex(16)
    review_token = secrets.token_hex(16)
    outbox_token = secrets.token_hex(16)
    client = TestClient(
        create_app(
            Store(),
            MockTriageModel(),
            webhook_secret=webhook_secret,
            review_token=review_token,
            outbox_token=outbox_token,
        )
    )
    auth = {"Authorization": f"Bearer {review_token}"}
    delivery = {"Authorization": f"Bearer {outbox_token}"}
    print("== 1. tickets arrive through the signed webhook")
    for t in TICKETS:
        body = json.dumps(t).encode()
        r = client.post(
            "/v1/webhooks/tickets",
            content=body,
            headers={
                signing.HEADER: signing.sign(body, webhook_secret),
                "content-type": "application/json",
            },
        )
        print(f"   {t['id']}: {r.status_code} {r.json()}")
    unsigned = client.post("/v1/webhooks/tickets", content=json.dumps(TICKETS[0]).encode())
    print(f"   unsigned request: {unsigned.status_code} {unsigned.json()['detail']}")

    print("== 2. what the model saw for demo-3 (personal data replaced)")
    pending = client.get("/v1/reviews?status=pending", headers=auth).json()
    third = next(p for p in pending if p["ticket_id"] == "demo-3")
    print("   " + third["triage"]["redacted_text"].replace("\n", "\n   "))

    print("== 3. a person reviews")
    for p in pending:
        tri = p["triage"]
        print(
            f"   review {p['id']} [{tri['route']}/{tri['priority']}] "
            f"escalations={tri['escalations']} draft={tri['status']}"
        )
    first = next(p for p in pending if p["ticket_id"] == "demo-1")
    second = next(p for p in pending if p["ticket_id"] == "demo-2")
    r = client.post(
        f"/v1/reviews/{third['id']}/approve", headers=auth, json={"reviewer": "agent.amy"}
    )
    print(f"   approve demo-3 as drafted: {r.status_code}")
    r = client.post(
        f"/v1/reviews/{first['id']}/approve",
        headers=auth,
        json={
            "reviewer": "agent.amy",
            "reply": "Hello [NAME_1],\n\nWe found the double charge and have "
            "started a refund.\n\nSupport team",
        },
    )
    print(f"   approve demo-1 with an edited reply: {r.status_code}")
    r = client.post(
        f"/v1/reviews/{second['id']}/reject",
        headers=auth,
        json={"reviewer": "agent.amy", "reason": "privacy office handles this"},
    )
    print(f"   reject demo-2 (handled by the privacy office): {r.status_code}")

    print("== 4. outbox: approved replies, personal data restored, waiting for delivery")
    for item in client.get("/v1/outbox", headers=delivery).json():
        print(
            f"   #{item['id']} -> {item['ticket_id']}:\n      "
            + item["reply"].replace("\n", "\n      ")
        )
    return 0
