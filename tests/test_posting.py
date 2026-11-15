"""Posting against the mock QM API, in-process, with scripted faults."""

import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from defect_automation.config import Settings
from defect_automation.db import utc_now
from defect_automation.pipeline import requeue_failed
from defect_automation.posting import (
    PostingBlocked,
    PostingError,
    TicketPayload,
    build_payload,
    idempotency_key,
    post_approved,
    post_report,
    post_with_retries,
)
from mock_qm.app import Chaos, create_app
from tests.conftest import VALID_ANSWER, add_report

TOKEN = "test-token"
CALM = Chaos(error_rate=0.0, spike_rate=0.0, spike_seconds=0.0, rate_limit_per_minute=1000)
FAULTY = Chaos(error_rate=0.5, spike_rate=0.0, spike_seconds=0.0, rate_limit_per_minute=1000)


class Scripted:
    """Feeds the mock's random draws: values below a rate trigger that kind of trouble."""

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)

    def random(self) -> float:
        return self._values.pop(0) if self._values else 0.99


# Per call the mock draws: spike?, error?, and if error on a POST: after-commit?
OK = [0.99, 0.99]
FAIL_BEFORE = [0.99, 0.0, 0.99]
FAIL_AFTER_COMMIT = [0.99, 0.0, 0.0]


def qm(chaos: Chaos = CALM, script: list[float] | None = None) -> TestClient:
    app = create_app(":memory:", TOKEN, chaos, Scripted(script or []))
    return TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})


def tickets_in_qm(client: TestClient) -> int:
    return client.app.state.count_notifications()


def approved_report(conn: sqlite3.Connection, file_hash: str = "hash-1", **changes: object):
    report = add_report(conn, file_hash=file_hash)
    fields = {k: v for k, v in {**VALID_ANSWER, **changes}.items() if k != "confidence"}
    conn.execute(
        "INSERT INTO extractions (report_id, fields_json, confidence_json, model, "
        "prompt_version, cost_eur, created_at) VALUES (?, ?, '{}', 'fake', 'v1', 0, ?)",
        (report["id"], json.dumps(fields), utc_now()),
    )
    conn.execute("UPDATE reports SET status = 'approved' WHERE id = ?", (report["id"],))
    conn.commit()
    return conn.execute("SELECT * FROM reports WHERE id = ?", (report["id"],)).fetchone()


def status(conn: sqlite3.Connection, report_id: int) -> str:
    return conn.execute("SELECT status FROM reports WHERE id = ?", (report_id,)).fetchone()[0]


def no_sleep(_: float) -> None:
    return None


PAYLOAD = TicketPayload(
    order_number="SG-2026-0003",
    part_number="BL-M10-0060",
    supplier="Wexmoor Fasteners Ltd",
    defect_type="dimensional",
    defect_description="Bolts too short.",
    quantity_affected=20,
    severity="minor",
    detected_at="incoming_inspection",
    detected_on="2026-11-03",
    reporter="Anna Keller",
    source_report="hash-1",
    approved_by="auto",
)


# --- the mock API contract -------------------------------------------------------------


def test_same_idempotency_key_returns_the_same_ticket() -> None:
    client = qm()
    first = client.post(
        "/api/v1/notifications", json=PAYLOAD.model_dump(), headers={"Idempotency-Key": "k1"}
    )
    second = client.post(
        "/api/v1/notifications", json=PAYLOAD.model_dump(), headers={"Idempotency-Key": "k1"}
    )

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]
    assert second.headers["Idempotent-Replay"] == "true"
    assert tickets_in_qm(client) == 1
    assert (
        client.get(f"/api/v1/notifications/{first.json()['id']}").json()["reporter"]
        == "Anna Keller"
    )


def test_api_rules() -> None:
    client = qm()
    body = PAYLOAD.model_dump()
    assert client.post("/api/v1/notifications", json=body).status_code == 400  # no key
    assert client.get("/api/v1/health").json() == {"status": "ok"}
    assert client.get("/api/v1/notifications/QN-000999").status_code == 404
    unauthorised = client.post(
        "/api/v1/notifications", json=body, headers={"Idempotency-Key": "k", "Authorization": "x"}
    )
    assert unauthorised.status_code == 401
    client.post("/api/v1/notifications", json=body, headers={"Idempotency-Key": "k1"})
    changed = client.post(
        "/api/v1/notifications",
        json={**body, "severity": "major"},
        headers={"Idempotency-Key": "k1"},
    )
    assert changed.status_code == 409


def test_tickets_cannot_be_edited_or_deleted() -> None:
    client = qm()
    created = client.post(
        "/api/v1/notifications", json=PAYLOAD.model_dump(), headers={"Idempotency-Key": "k1"}
    )
    url = f"/api/v1/notifications/{created.json()['id']}"
    assert client.put(url, json={}).status_code == 405
    assert client.patch(url, json={}).status_code == 405
    assert client.delete(url).status_code == 405


def test_rate_limit_answers_429_with_retry_after() -> None:
    client = qm(Chaos(0.0, 0.0, 0.0, rate_limit_per_minute=2))
    codes = [
        client.post(
            "/api/v1/notifications", json=PAYLOAD.model_dump(), headers={"Idempotency-Key": f"k{i}"}
        )
        for i in range(3)
    ]
    assert [r.status_code for r in codes] == [201, 201, 429]
    assert int(codes[2].headers["Retry-After"]) >= 1


# --- retries ---------------------------------------------------------------------------


def test_retries_survive_503_errors() -> None:
    client = qm(FAULTY, FAIL_BEFORE + FAIL_BEFORE + OK)
    waits: list[float] = []
    result = post_with_retries(client, PAYLOAD, "k1", 3, 0.5, waits.append)

    assert result.attempts == 3
    assert tickets_in_qm(client) == 1
    assert len(waits) == 2 and waits[1] > waits[0]  # backoff grows


def test_lost_response_after_save_does_not_create_a_second_ticket() -> None:
    client = qm(FAULTY, FAIL_AFTER_COMMIT + OK)
    result = post_with_retries(client, PAYLOAD, "k1", 3, 0.5, no_sleep)

    assert result.attempts == 2
    assert result.replayed  # the QM system already had it
    assert tickets_in_qm(client) == 1


def test_gives_up_after_three_retries() -> None:
    client = qm(FAULTY, FAIL_BEFORE * 4)
    attempts: list[str] = []
    with pytest.raises(PostingError, match="after 3 retries"):
        post_with_retries(client, PAYLOAD, "k1", 3, 0.5, no_sleep, lambda n, o: attempts.append(o))
    assert attempts == ["HTTP 503"] * 4
    assert tickets_in_qm(client) == 0


def test_client_error_is_not_retried() -> None:
    client = qm()
    client.headers["Authorization"] = "Bearer wrong"
    attempts: list[int] = []
    with pytest.raises(PostingError, match="rejected"):
        post_with_retries(client, PAYLOAD, "k1", 3, 0.5, no_sleep, lambda n, o: attempts.append(n))
    assert attempts == [1]


def test_rate_limit_wait_follows_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        handler.calls += 1
        if handler.calls == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(201, json={"id": "QN-000001"})

    handler.calls = 0
    waits: list[float] = []
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://qm")
    assert post_with_retries(http, PAYLOAD, "k1", 3, 0.5, waits.append).ticket_id == "QN-000001"
    assert waits == [7.0]


def test_network_errors_are_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        handler.calls += 1
        if handler.calls < 3:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(201, json={"id": "QN-000001"})

    handler.calls = 0
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://qm")
    assert post_with_retries(http, PAYLOAD, "k1", 3, 0.5, no_sleep).attempts == 3


# --- posting a report ------------------------------------------------------------------


def test_same_report_posted_twice_gives_one_ticket(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    client = qm()
    report = approved_report(conn)
    first = post_report(conn, client, settings, "run-1", report, no_sleep)
    second = post_report(conn, client, settings, "run-2", report, no_sleep)

    assert first == second == "QN-000001"
    assert tickets_in_qm(client) == 1
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1
    assert status(conn, report["id"]) == "posted"
    assert post_approved(conn, client, settings, "run-3", no_sleep) == {}  # nothing left to post


def test_crash_after_the_api_answered_is_healed_on_rerun(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    client = qm()
    report = approved_report(conn)
    post_report(conn, client, settings, "run-1", report, no_sleep)
    # Simulate the crash: the QM system has the ticket, our database lost it.
    conn.execute("DELETE FROM tickets")
    conn.execute("UPDATE reports SET status = 'approved'")
    conn.commit()

    assert post_approved(conn, client, settings, "run-2", no_sleep) == {"sample.txt": "QN-000001"}
    assert tickets_in_qm(client) == 1


def test_ticket_carries_the_real_reporter_name(conn: sqlite3.Connection) -> None:
    payload = build_payload(conn, approved_report(conn))
    assert payload.reporter == "Anna Keller"
    assert payload.approved_by == "auto"
    assert payload.source_report == "hash-1"
    stored = conn.execute("SELECT fields_json FROM extractions").fetchone()[0]
    assert json.loads(stored)["reporter"] == "[PERSON_1]"  # the database copy stays masked


def test_idempotency_key_comes_from_the_file_hash() -> None:
    assert idempotency_key("abc") == "report-abc"


def test_critical_report_is_never_posted_without_a_human_approval(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    client = qm()
    report = approved_report(conn, severity="critical")  # 'approved' by mistake
    with pytest.raises(PostingBlocked):
        build_payload(conn, report)

    assert post_report(conn, client, settings, "run-1", report, no_sleep) is None
    assert tickets_in_qm(client) == 0
    assert status(conn, report["id"]) == "in_review"


def test_critical_report_posts_after_a_human_approval_with_the_edited_fields(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    client = qm()
    report = approved_report(conn, severity="critical")
    edited = {k: v for k, v in VALID_ANSWER.items() if k != "confidence"}
    edited.update(severity="critical", quantity_affected=3)
    conn.execute(
        "INSERT INTO reviews (report_id, reviewer, action, edited_fields_json, reviewed_at) "
        "VALUES (?, 'faisal', 'approve', ?, ?)",
        (report["id"], json.dumps(edited), utc_now()),
    )
    conn.commit()

    ticket_id = post_report(conn, client, settings, "run-1", report, no_sleep)
    posted = client.get(f"/api/v1/notifications/{ticket_id}").json()
    assert posted["approved_by"] == "faisal"
    assert posted["quantity_affected"] == 3
    assert posted["severity"] == "critical"


def test_failed_posting_lands_in_the_error_queue_and_can_be_requeued(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    client = qm(FAULTY, FAIL_BEFORE * 4)
    report = approved_report(conn)
    assert post_report(conn, client, settings, "run-1", report, no_sleep) is None
    assert status(conn, report["id"]) == "failed"
    attempts = conn.execute("SELECT COUNT(*) FROM audit_log WHERE event = 'post_attempt'")
    assert attempts.fetchone()[0] == 4

    assert requeue_failed(conn, "run-2") == 1
    assert status(conn, report["id"]) == "approved"  # back to posting, not to extraction
    assert post_approved(conn, client, settings, "run-2", no_sleep) == {"sample.txt": "QN-000001"}
