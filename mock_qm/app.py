"""Mock QM system. Stands in for the real quality management API (SAP QM in phase 2).

It can create and read notifications, nothing else: no edit, no close, no delete.
It misbehaves on purpose so the client's retry and idempotency logic can be proven:

- a share of calls fail with 503, half of them AFTER the ticket was saved (the response
  is lost, the ticket exists: the case an idempotency key is for)
- some calls take a second longer
- more than N calls per minute get a 429 with a Retry-After header

Run: python -m defect_automation qm-api
"""

import json
import random
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict


class RandomSource(Protocol):
    def random(self) -> float: ...


@dataclass(frozen=True)
class Chaos:
    error_rate: float = 0.05
    spike_rate: float = 0.05
    spike_seconds: float = 1.0
    rate_limit_per_minute: int = 60


class NotificationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_number: str | None
    part_number: str | None
    supplier: str | None
    defect_type: str
    defect_description: str
    quantity_affected: int | None
    severity: str
    detected_at: str | None
    detected_on: str | None
    reporter: str | None
    source_report: str
    approved_by: str


class NotificationOut(NotificationIn):
    id: str
    status: str
    created_at: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    number          INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""


def _ticket_id(number: int) -> str:
    return f"QN-{number:06d}"


def _to_out(row: sqlite3.Row) -> NotificationOut:
    return NotificationOut(
        id=_ticket_id(row["number"]),
        status="open",
        created_at=row["created_at"],
        **json.loads(row["payload_json"]),
    )


def create_app(
    db_path: Path | str,
    api_token: str,
    chaos: Chaos | None = None,
    rng: RandomSource | None = None,
) -> FastAPI:
    chaos = chaos or Chaos()
    rng = rng or random.Random()
    app = FastAPI(title="Mock QM API", version="1.0")
    lock = threading.Lock()
    calls: deque[float] = deque()

    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # One shared connection guarded by a lock keeps the mock simple and thread-safe.
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)

    def authorise(authorization: Annotated[str | None, Header()] = None) -> None:
        if authorization != f"Bearer {api_token}":
            raise HTTPException(401, "missing or wrong service account token")

    def trouble(request: Request) -> None:
        """Rate limit, latency spike and the 'fail before doing anything' kind of 503."""
        now = time.monotonic()
        with lock:
            while calls and now - calls[0] >= 60:
                calls.popleft()
            if len(calls) >= chaos.rate_limit_per_minute:
                retry_after = max(1, int(60 - (now - calls[0])) + 1)
                raise HTTPException(
                    429, "rate limit exceeded", headers={"Retry-After": str(retry_after)}
                )
            calls.append(now)
        if rng.random() < chaos.spike_rate:
            time.sleep(chaos.spike_seconds)
        request.state.fail_after_commit = False
        if rng.random() < chaos.error_rate:
            if request.method == "POST" and rng.random() < 0.5:
                request.state.fail_after_commit = True
            else:
                raise HTTPException(503, "service temporarily unavailable")

    guarded = [Depends(authorise), Depends(trouble)]

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/notifications", dependencies=guarded, status_code=201)
    def create_notification(
        body: NotificationIn,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> NotificationOut:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        payload = body.model_dump_json()
        with lock:
            row = db.execute(
                "SELECT * FROM notifications WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            replay = row is not None
            if replay:
                if json.loads(row["payload_json"]) != json.loads(payload):
                    raise HTTPException(409, "Idempotency-Key was already used with another body")
            else:
                db.execute(
                    "INSERT INTO notifications (idempotency_key, payload_json, created_at) "
                    "VALUES (?, ?, ?)",
                    (idempotency_key, payload, datetime.now(UTC).isoformat(timespec="seconds")),
                )
                db.commit()
                row = db.execute(
                    "SELECT * FROM notifications WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
        if request.state.fail_after_commit:
            raise HTTPException(503, "service temporarily unavailable")  # saved, answer lost
        if replay:
            response.status_code = 200
            response.headers["Idempotent-Replay"] = "true"
        return _to_out(row)

    @app.get("/api/v1/notifications/{ticket_id}", dependencies=guarded)
    def get_notification(ticket_id: str) -> NotificationOut:
        number = ticket_id.removeprefix("QN-")
        with lock:
            row = (
                db.execute(
                    "SELECT * FROM notifications WHERE number = ?", (int(number),)
                ).fetchone()
                if number.isdigit()
                else None
            )
        if row is None:
            raise HTTPException(404, "notification not found")
        return _to_out(row)

    def count() -> int:
        with lock:
            return int(db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0])

    app.state.count_notifications = count  # for tests and demos; not an API endpoint
    return app


def app_from_settings() -> FastAPI:
    """Factory used by uvicorn. Reads the same .env as the pipeline."""
    from defect_automation.config import get_settings

    settings = get_settings()
    return create_app(
        settings.mock_qm_db_path,
        settings.qm_api_token.get_secret_value(),
        Chaos(
            error_rate=settings.mock_qm_error_rate,
            spike_rate=settings.mock_qm_spike_rate,
            spike_seconds=settings.mock_qm_spike_seconds,
            rate_limit_per_minute=settings.mock_qm_rate_limit_per_minute,
        ),
    )
