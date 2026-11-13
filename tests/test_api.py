import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from defect_automation.api import create_app
from defect_automation.config import Settings
from defect_automation.db import connect, init_db
from defect_automation.seed import seed_master_data
from tests.conftest import SAMPLE_TXT, VALID_ANSWER, FakeLLM

AUTH = {"Authorization": "Bearer test-token"}


def answer(**changes: object) -> str:
    return json.dumps({**VALID_ANSWER, **changes})


@pytest.fixture
def api_settings(settings: Settings, tmp_path: Path) -> Settings:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    conn = connect(settings.db_path)
    init_db(conn)
    seed_master_data(conn, settings.master_dir)
    conn.close()
    return settings.model_copy(
        update={"input_dir": inbox, "pipeline_api_token": SecretStr("test-token")}
    )


def client(settings: Settings, answers: list[str | Exception]) -> TestClient:
    llm = FakeLLM(answers)
    # No HTTP client: the posting step is skipped, as in the pipeline tests.
    return TestClient(create_app(settings, llm_factory=lambda: llm, http_factory=lambda: None))


def drop(settings: Settings, name: str, text: str = SAMPLE_TXT) -> None:
    (settings.input_dir / name).write_text(text, encoding="utf-8")


def test_health_needs_no_token(api_settings: Settings) -> None:
    assert client(api_settings, []).get("/health").json() == {"status": "ok"}


def test_run_without_or_with_wrong_token_is_refused(api_settings: Settings) -> None:
    drop(api_settings, "a.txt")
    api = client(api_settings, [])  # a model call would raise
    assert api.post("/runs").status_code == 401
    assert api.post("/runs", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert api.get("/status").status_code == 401


def test_api_stays_closed_when_no_token_is_configured(api_settings: Settings) -> None:
    closed = api_settings.model_copy(update={"pipeline_api_token": None})
    assert client(closed, []).post("/runs", headers=AUTH).status_code == 503


def test_run_reports_counts_and_flags_review_work(api_settings: Settings) -> None:
    drop(api_settings, "a_clean.txt")
    drop(api_settings, "b_critical.txt", SAMPLE_TXT + "\nsecond file")
    api = client(api_settings, [answer(), answer(severity="critical")])

    body = api.post("/runs", headers=AUTH).json()
    assert body["new_reports"] == 2
    assert body["auto_approved"] == 1
    assert body["sent_to_review"] == 1
    assert body["waiting_for_review"] == 1
    assert body["failed"] == 0
    assert body["needs_attention"] is True
    assert body["review_url"] == "http://localhost:8501"


def test_second_run_is_a_no_op(api_settings: Settings) -> None:
    drop(api_settings, "a.txt")
    api = client(api_settings, [answer()])
    api.post("/runs", headers=AUTH)

    body = api.post("/runs", headers=AUTH).json()  # an extra model call would raise
    assert (body["new_reports"], body["duplicates_skipped"]) == (0, 1)
    assert body["needs_attention"] is False


def test_failed_report_is_counted(api_settings: Settings) -> None:
    drop(api_settings, "a.txt")
    body = client(api_settings, [TimeoutError("slow")]).post("/runs", headers=AUTH).json()
    assert body["failed"] == 1
    assert body["needs_attention"] is True


def test_answer_holds_no_report_text_or_names(api_settings: Settings) -> None:
    drop(api_settings, "a.txt")
    text = client(api_settings, [answer(severity="critical")]).post("/runs", headers=AUTH).text
    assert "Anna" not in text and "Keller" not in text and "gasket" not in text


def test_status_lists_counts(api_settings: Settings) -> None:
    drop(api_settings, "a.txt")
    api = client(api_settings, [answer(severity="critical")])
    api.post("/runs", headers=AUTH)

    body = api.get("/status", headers=AUTH).json()
    assert body["reports_by_status"] == {"in_review": 1}
    assert body["waiting_for_review"] == 1
    assert body["tickets"] == 0
