"""Click through the Streamlit app with Streamlit's own test driver."""

import json
import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from defect_automation.config import get_settings
from defect_automation.db import connect, init_db
from defect_automation.pipeline import run_pipeline
from defect_automation.seed import seed_master_data
from tests.conftest import ROOT, SAMPLE_TXT, VALID_ANSWER, FakeLLM


@pytest.fixture
def app_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """A file database with one critical report in review, and the app pointed at it."""
    db_path = tmp_path / "app.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("REVIEW_PASSWORD", "open-sesame")
    monkeypatch.setenv("QM_API_URL", "http://127.0.0.1:9")  # nothing listens here
    monkeypatch.chdir(ROOT)
    get_settings.cache_clear()
    settings = get_settings()

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "critical.txt").write_text(SAMPLE_TXT, encoding="utf-8")
    conn = connect(db_path)
    init_db(conn)
    seed_master_data(conn, settings.master_dir)
    llm = FakeLLM([json.dumps({**VALID_ANSWER, "severity": "critical"})])
    run_pipeline(conn, llm, settings.model_copy(update={"input_dir": inbox}), "run-1")
    yield conn
    get_settings.cache_clear()


def login(password: str = "open-sesame") -> AppTest:
    app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30).run()
    app.text_input[0].set_value("Faisal")
    app.text_input[1].set_value(password)
    app.button[0].click().run()
    return app


def test_wrong_password_shows_no_report_data(app_db: sqlite3.Connection) -> None:
    app = login("wrong")
    assert [e.value for e in app.error] == ["Enter your name and the correct password."]
    assert not app.code and not app.selectbox


def test_app_refuses_to_open_without_a_configured_password(
    app_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEW_PASSWORD", "")
    get_settings.cache_clear()
    app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30).run()
    assert "No review password is set" in app.error[0].value
    assert not app.text_input


def test_reviewer_sees_the_report_edits_a_field_and_approves(app_db: sqlite3.Connection) -> None:
    app = login()
    assert not app.exception
    assert app.title[0].value == "Review queue"
    assert [w.value for w in app.warning] == ["severity is critical"]
    assert "At incoming inspection" in app.code[0].value

    app.number_input[0].set_value(7)
    approve = next(b for b in app.button if b.label == "Approve and post")
    approve.click().run()

    assert not app.exception
    assert "approved, but the QM API did not take it" in app.success[0].value
    assert app_db.execute("SELECT status FROM reports").fetchone()[0] == "approved"
    review = app_db.execute("SELECT * FROM reviews").fetchone()
    assert review["reviewer"] == "Faisal"
    assert json.loads(review["edited_fields_json"])["quantity_affected"] == 7
    assert "Nothing is waiting for review." in [i.value for i in app.info]


def test_metrics_page_renders(app_db: sqlite3.Connection) -> None:
    app = login()
    app.sidebar.radio[0].set_value("Metrics").run()

    assert not app.exception
    assert app.title[0].value == "Metrics"
    tiles = {m.label: m.value for m in app.metric}
    assert tiles["Reports received"] == "1"
    assert tiles["Review rate"] == "100%"
    assert "Field accuracy" in tiles  # read from the saved evaluation run in the repo
