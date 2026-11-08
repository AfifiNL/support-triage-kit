"""Review queue and outbox in SQLite (standard library only).

Every triaged ticket becomes a review. Only a person can move it on: approve (optionally with an
edited reply) or reject. An approved reply lands in the outbox with personal data restored, for
the helpdesk integration to send and acknowledge. Nothing in this package sends anything.
Every state change is written to an append-only event log.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from triage_kit.models import TriageResult
from triage_kit.redact import RESTORABLE, placeholders_in, restore

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY,
    ticket_id     TEXT NOT NULL UNIQUE,
    body_sha256   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    triage_json   TEXT NOT NULL,
    pii_map_json  TEXT NOT NULL,
    final_reply   TEXT,
    reviewer      TEXT,
    decided_at    TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY,
    review_id  INTEGER NOT NULL UNIQUE REFERENCES reviews(id),
    ticket_id  TEXT NOT NULL,
    reply      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acked_at   TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY,
    review_id INTEGER NOT NULL REFERENCES reviews(id),
    at        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT ''
);
CREATE TRIGGER IF NOT EXISTS events_append_only_u BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_append_only_d BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


class ReviewError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _same(text: str, candidates: list[str]) -> bool:
    norm = " ".join(text.split())
    return any(norm == " ".join(c.split()) for c in candidates if c)


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def _event(self, review_id: int, actor: str, action: str, detail: str = "") -> None:
        self._db.execute(
            "INSERT INTO events (review_id, at, actor, action, detail) VALUES (?, ?, ?, ?, ?)",
            (review_id, _now(), actor, action, detail),
        )

    def find(self, ticket_id: str) -> tuple[int, str] | None:
        """(review id, body hash) of a ticket already stored, so a redelivery can be answered
        without calling the model again."""
        with self._lock:
            row = self._db.execute(
                "SELECT id, body_sha256 FROM reviews WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            return None if row is None else (int(row["id"]), str(row["body_sha256"]))

    def add(
        self, result: TriageResult, pii_map: dict[str, str], body_sha256: str
    ) -> tuple[int, bool]:
        """Store a triaged ticket. Returns (review id, created); a known ticket id is not
        stored twice (webhooks are delivered at least once)."""
        with self._lock:
            row = self._db.execute(
                "SELECT id FROM reviews WHERE ticket_id = ?", (result.ticket_id,)
            ).fetchone()
            if row:
                return int(row["id"]), False
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cur = self._db.execute(
                    "INSERT INTO reviews (ticket_id, body_sha256, created_at, status, triage_json,"
                    " pii_map_json) VALUES (?, ?, ?, 'pending', ?, ?)",
                    (
                        result.ticket_id,
                        body_sha256,
                        _now(),
                        result.model_dump_json(),
                        json.dumps(pii_map),
                    ),
                )
                rid = int(cur.lastrowid or 0)
                self._event(rid, "system", "triaged", f"{result.route}/{result.priority.value}")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            return rid, True

    def _review(self, rid: int) -> sqlite3.Row:
        row: sqlite3.Row | None = self._db.execute(
            "SELECT * FROM reviews WHERE id = ?", (rid,)
        ).fetchone()
        if row is None:
            raise ReviewError(404, f"review {rid} not found")
        return row

    def view(self, rid: int) -> dict[str, Any]:
        with self._lock:
            return self._as_view(self._review(rid))

    @staticmethod
    def _as_view(row: sqlite3.Row) -> dict[str, Any]:
        triage = json.loads(row["triage_json"])
        pii = json.loads(row["pii_map_json"])
        draft = triage.get("draft")
        return {
            "id": row["id"],
            "ticket_id": row["ticket_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "triage": triage,
            # The reviewer is staff and sees the reply as the customer would receive it.
            "draft_for_reviewer": restore(draft, pii) if draft else None,
            "final_reply": row["final_reply"],
            "reviewer": row["reviewer"],
            "decided_at": row["decided_at"],
        }

    def reviews(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._db.execute(
                    "SELECT * FROM reviews WHERE status = ? ORDER BY id", (status,)
                ).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM reviews ORDER BY id").fetchall()
            return [self._as_view(r) for r in rows]

    def approve(self, rid: int, reviewer: str, edited_reply: str | None = None) -> dict[str, Any]:
        with self._lock:
            row = self._review(rid)
            if row["status"] != "pending":
                raise ReviewError(409, f"review {rid} is already {row['status']}")
            triage = json.loads(row["triage_json"])
            pii = json.loads(row["pii_map_json"])
            draft = triage.get("draft") or ""
            flagged = triage["status"] != "ready_for_review" or not draft
            edited = (edited_reply or "").strip()
            if edited and _same(edited, [draft, restore(draft, pii)]):
                edited = ""  # resubmitting the draft unchanged is not an edit
            if not edited:
                if flagged:
                    raise ReviewError(
                        422,
                        "this draft failed its checks or is missing; approve with an edited reply",
                    )
                reply, action = restore(draft, pii), "approved"
            else:
                blocked = sorted(
                    p for p in placeholders_in(edited) if p[1:].split("_")[0] not in RESTORABLE
                )
                if blocked:
                    raise ReviewError(
                        422, f"card numbers, IBANs and IP addresses are never sent: {blocked}"
                    )
                reply, action = restore(edited, pii), "approved_with_edits"
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cur = self._db.execute(
                    "UPDATE reviews SET status = 'approved', final_reply = ?, reviewer = ?,"
                    " decided_at = ? WHERE id = ? AND status = 'pending'",
                    (reply, reviewer, _now(), rid),
                )
                if cur.rowcount != 1:
                    raise ReviewError(409, f"review {rid} was decided concurrently")
                self._db.execute(
                    "INSERT INTO outbox (review_id, ticket_id, reply, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (rid, row["ticket_id"], reply, _now()),
                )
                self._event(rid, reviewer, action)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            return self._as_view(self._review(rid))

    def reject(self, rid: int, reviewer: str, reason: str = "") -> dict[str, Any]:
        with self._lock:
            row = self._review(rid)
            if row["status"] != "pending":
                raise ReviewError(409, f"review {rid} is already {row['status']}")
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cur = self._db.execute(
                    "UPDATE reviews SET status = 'rejected', reviewer = ?, decided_at = ?"
                    " WHERE id = ? AND status = 'pending'",
                    (reviewer, _now(), rid),
                )
                if cur.rowcount != 1:
                    raise ReviewError(409, f"review {rid} was decided concurrently")
                self._event(rid, reviewer, "rejected", reason)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            return self._as_view(self._review(rid))

    def outbox(self, include_acked: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            if include_acked:
                rows = self._db.execute("SELECT * FROM outbox ORDER BY id").fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM outbox WHERE acked_at IS NULL ORDER BY id"
                ).fetchall()
            return [dict(r) for r in rows]

    def ack(self, outbox_id: int) -> None:
        with self._lock:
            cur = self._db.execute(
                "UPDATE outbox SET acked_at = ? WHERE id = ? AND acked_at IS NULL",
                (_now(), outbox_id),
            )
            if cur.rowcount == 0:
                exists = self._db.execute(
                    "SELECT 1 FROM outbox WHERE id = ?", (outbox_id,)
                ).fetchone()
                if not exists:
                    raise ReviewError(404, f"outbox item {outbox_id} not found")

    def forget(self, rid: int, actor: str) -> None:
        """Erase the personal data held for one review (a data-protection erasure request):
        the placeholder map, the restored replies, and the texts that may contain names the
        redaction missed. Routing facts and the event log stay."""
        with self._lock:
            row = self._review(rid)
            triage = json.loads(row["triage_json"])
            for field in ("redacted_text", "draft", "summary"):
                if triage.get(field):
                    triage[field] = "[erased]"
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "UPDATE reviews SET pii_map_json = '{}', triage_json = ?,"
                    " final_reply = CASE WHEN final_reply IS NULL THEN NULL ELSE '[erased]' END"
                    " WHERE id = ?",
                    (json.dumps(triage), rid),
                )
                self._db.execute("UPDATE outbox SET reply = '[erased]' WHERE review_id = ?", (rid,))
                self._event(rid, actor, "erased")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def events(self, rid: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT at, actor, action, detail FROM events WHERE review_id = ? ORDER BY id",
                (rid,),
            ).fetchall()
            return [dict(r) for r in rows]
