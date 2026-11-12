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
    monkeypatch.setenv("REVIEW_LOGIN_OPEN", "false")  # whatever the local .env says
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


def click(app: AppTest, label: str) -> None:
    next(b for b in app.button if b.label == label).click().run()


def test_cancel_in_the_popup_saves_nothing(app_db: sqlite3.Connection) -> None:
    app = login()
    # The colour rules hang on these keys: green for approve, red for reject.
    keys = {b.label: b.key for b in app.button}
    assert keys["Approve and post"] == "approve_button"
    assert keys["Reject"] == "reject_button"
    click(app, "Reject")
    assert "Are you sure you want to reject **critical.txt**?" in [m.value for m in app.markdown]
    assert {b.label: b.key for b in app.button}["Yes, reject"] == "confirm_reject_button"
    click(app, "Cancel")

    assert not app.exception
    assert "Cancel" not in [b.label for b in app.button]  # the popup is closed
    assert app_db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    assert app_db.execute("SELECT status FROM reports").fetchone()[0] == "in_review"


def test_reject_needs_a_yes_in_the_popup(app_db: sqlite3.Connection) -> None:
    app = login()
    click(app, "Reject")
    assert app_db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    click(app, "Yes, reject")

    assert not app.exception
    assert "critical.txt rejected. No ticket was created." in [s.value for s in app.success]
    assert app_db.execute("SELECT status FROM reports").fetchone()[0] == "rejected"
    assert app_db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_demo_mode_accepts_any_password_but_still_needs_a_name(
    app_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEW_LOGIN_OPEN", "true")
    get_settings.cache_clear()

    app = login("anything at all")
    assert not app.exception
    assert app.title[0].value == "Review queue"

    nameless = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30).run()
    assert "Demo mode" in nameless.warning[0].value
    nameless.text_input[1].set_value("x")
    nameless.button[0].click().run()
    assert [e.value for e in nameless.error] == ["Enter your name."]
    assert not nameless.code


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
    click(app, "Approve and post")

    # Nothing is saved until the reviewer confirms in the popup.
    assert "Are you sure you want to approve **critical.txt**?" in [m.value for m in app.markdown]
    assert "This is a critical defect." in [w.value for w in app.warning]
    assert app_db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    click(app, "Yes, approve and post")

    assert not app.exception
    assert "approved, but the QM API did not take it" in app.success[0].value
    assert app_db.execute("SELECT status FROM reports").fetchone()[0] == "approved"
    review = app_db.execute("SELECT * FROM reviews").fetchone()
    assert review["reviewer"] == "Faisal"
    assert json.loads(review["edited_fields_json"])["quantity_affected"] == 7
    assert "Nothing is waiting for review." in [i.value for i in app.info]


def test_report_with_a_missing_field_opens_and_can_be_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a null order number made the form crash with an AttributeError."""
    db_path = tmp_path / "app.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("REVIEW_PASSWORD", "open-sesame")
    monkeypatch.setenv("REVIEW_LOGIN_OPEN", "false")  # whatever the local .env says
    monkeypatch.setenv("QM_API_URL", "http://127.0.0.1:9")
    monkeypatch.chdir(ROOT)
    get_settings.cache_clear()
    settings = get_settings()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "no_order.txt").write_text(SAMPLE_TXT, encoding="utf-8")
    conn = connect(db_path)
    init_db(conn)
    seed_master_data(conn, settings.master_dir)
    nulls = {"order_number": None, "supplier": None, "defect_description": None, "reporter": None}
    llm = FakeLLM([json.dumps({**VALID_ANSWER, **nulls})])
    run_pipeline(conn, llm, settings.model_copy(update={"input_dir": inbox}), "run-1")

    app = login()
    assert not app.exception
    assert "order_number is missing" in [w.value for w in app.warning]
    order = next(t for t in app.text_input if t.label.startswith("Order number"))
    assert order.value == ""

    # Approving with the description still empty is refused with a message, not a crash,
    # and no confirmation popup opens.
    click(app, "Approve and post")
    assert not app.exception
    assert "Not saved" in app.error[0].value
    assert "Yes, approve and post" not in [b.label for b in app.button]

    order = next(t for t in app.text_input if t.label.startswith("Order number"))
    order.set_value("SG-2026-0003")
    app.text_area[0].set_value("Scratches on the gaskets.")
    click(app, "Approve and post")
    click(app, "Yes, approve and post")
    assert not app.exception
    stored = json.loads(conn.execute("SELECT edited_fields_json FROM reviews").fetchone()[0])
    assert stored["order_number"] == "SG-2026-0003"
    assert stored["supplier"] is None
    get_settings.cache_clear()


def test_tickets_page_lists_posted_tickets_with_their_content(app_db: sqlite3.Connection) -> None:
    app = login()
    app.sidebar.radio[0].set_value("Tickets").run()
    assert "No tickets have been posted yet." in [i.value for i in app.info]

    # Approve the report by hand and record a ticket for it, as a successful post would.
    app_db.execute(
        "INSERT INTO reviews (report_id, reviewer, action, reviewed_at) "
        "VALUES (1, 'Faisal', 'approve', '2026-11-11T10:00:00+00:00')"
    )
    app_db.execute(
        "INSERT INTO tickets (report_id, qm_ticket_id, posted_via, posted_at) "
        "VALUES (1, 'QN-000042', 'api', '2026-11-11T10:00:01+00:00')"
    )
    app_db.execute("UPDATE reports SET status = 'posted'")
    app_db.commit()
    app.run()

    assert not app.exception
    listing = app.dataframe[0].value
    assert list(listing["Ticket"]) == ["QN-000042"]
    assert list(listing["Approved"]) == ["human review"]
    assert list(listing["Severity"]) == ["critical"]
    details = app.dataframe[1].value
    assert "Anna Keller" in list(details["Value"])  # the ticket carries the real reporter
    assert "At incoming inspection" in app.code[0].value


def test_metrics_page_renders(app_db: sqlite3.Connection) -> None:
    app = login()
    app.sidebar.radio[0].set_value("Metrics").run()

    assert not app.exception
    assert app.title[0].value == "Metrics"
    tiles = {m.label: m.value for m in app.metric}
    assert tiles["Reports received"] == "1"
    assert tiles["Review rate"] == "100%"
    assert "Field accuracy" in tiles  # read from the saved evaluation run in the repo
