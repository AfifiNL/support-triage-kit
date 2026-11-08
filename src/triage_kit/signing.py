"""HMAC-SHA256 webhook signatures with a timestamp, so a captured request cannot be replayed
later. Header format: `X-Triage-Signature: t=<unix seconds>,v1=<hex digest>`, where the digest
covers `<t>.<raw body>`."""

from __future__ import annotations

import hashlib
import hmac
import time

HEADER = "X-Triage-Signature"
TOLERANCE_S = 300


class SignatureError(ValueError):
    pass


def sign(body: bytes, secret: str, timestamp: int | None = None) -> str:
    t = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify(body: bytes, header: str | None, secret: str, now: float | None = None) -> None:
    if not header:
        raise SignatureError("missing signature header")
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        t = int(parts["t"])
        given = parts["v1"]
    except (KeyError, ValueError):
        raise SignatureError("malformed signature header") from None
    current = time.time() if now is None else now
    if abs(current - t) > TOLERANCE_S:
        raise SignatureError("signature timestamp outside the allowed window")
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), given.encode("utf-8", "replace")):
        raise SignatureError("signature does not match")
