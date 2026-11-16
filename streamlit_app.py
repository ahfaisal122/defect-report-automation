"""Review screen and metrics page.

Run: streamlit run streamlit_app.py
"""

import hmac
import html
import json
import re
import sqlite3
from datetime import date

import altair as alt
import pandas as pd
import streamlit as st
from pydantic import ValidationError
from streamlit.delta_generator import DeltaGenerator

from defect_automation.config import get_settings
from defect_automation.db import connect, init_db, new_run_id
from defect_automation.domain import DefectType, Severity
from defect_automation.logging_setup import configure_logging
from defect_automation.metrics import Metrics, compute_metrics
from defect_automation.posting import (
    AUTO_APPROVER,
    list_tickets,
    make_http_client,
    post_report,
    qm_available,
)
from defect_automation.review import (
    APPROVE,
    REJECT,
    ReviewError,
    ReviewItem,
    check_approvable,
    list_rejected,
    load_review_item,
    review_day,
    review_queue,
    submit_review,
)
from defect_automation.schemas import ExtractedFields

# The one accent colour: charts here, buttons and menu via .streamlit/config.toml.
SERIES_COLOR = "#2a78d6"
FIELD_LABELS = {
    "order_number": "Order number",
    "part_number": "Part number",
    "supplier": "Supplier",
    "defect_type": "Defect type",
    "defect_description": "Defect description",
    "quantity_affected": "Quantity affected",
    "severity": "Severity",
    "detected_at": "Detected at (station)",
    "detected_on": "Detected on",
    "reporter": "Reporter",
    "reviewer_note": "Reviewer note",
}

# The number boxes come in four tones. The tone follows the meaning of the number, the
# same on every page: blue = information, amber = waiting for a person, green = done,
# red = rejected. Each tone is a pale fill with a strong bar on the left.
NAVY, CARD_BORDER = "#14294a", "#d5deeb"
TILE_TONES = {
    "blue": ("#e8f1fd", SERIES_COLOR),
    "amber": ("#fff3d6", "#d98e00"),
    "green": ("#e4f4e8", "#1a7f37"),
    "red": ("#fde8e8", "#c62828"),
}
TILE_CSS = "\n".join(
    f'[class*="st-key-tile_{tone}_"] [data-testid="stMetric"] {{'
    f" background-color: {fill}; border-left: 6px solid {bar}; border-radius: 0.6rem;"
    " padding: 0.9rem 1rem; box-shadow: 0 1px 2px rgba(20, 41, 74, 0.08); }"
    for tone, (fill, bar) in TILE_TONES.items()
)

# Approve is green and Reject is red, on the form and in the confirmation popup.
# Streamlit gives a widget with key "x" the CSS class "st-key-x". The labels still say
# what each button does, so the meaning never depends on colour alone.
APPROVE_GREEN, APPROVE_GREEN_HOVER = "#1a7f37", "#156a2d"
REJECT_RED, REJECT_RED_HOVER = "#c62828", "#a82020"
APP_CSS = f"""
<style>
.st-key-approve_button button, .st-key-confirm_approve_button button {{
    background-color: {APPROVE_GREEN}; border-color: {APPROVE_GREEN}; color: #ffffff;
}}
.st-key-approve_button button:hover, .st-key-confirm_approve_button button:hover,
.st-key-approve_button button:focus-visible, .st-key-confirm_approve_button button:focus-visible {{
    background-color: {APPROVE_GREEN_HOVER}; border-color: {APPROVE_GREEN_HOVER}; color: #ffffff;
}}
.st-key-reject_button button, .st-key-confirm_reject_button button {{
    background-color: {REJECT_RED}; border-color: {REJECT_RED}; color: #ffffff;
}}
.st-key-reject_button button:hover, .st-key-confirm_reject_button button:hover,
.st-key-reject_button button:focus-visible, .st-key-confirm_reject_button button:focus-visible {{
    background-color: {REJECT_RED_HOVER}; border-color: {REJECT_RED_HOVER}; color: #ffffff;
}}
{TILE_CSS}
h1 {{
    color: {NAVY}; border-bottom: 3px solid {SERIES_COLOR};
    padding-bottom: 0.4rem;
}}
[data-testid="stHeading"]:has(h1) {{ margin-bottom: 0.75rem; }}
h3 {{ color: {NAVY}; }}
/* Long box titles wrap onto a second line; they are never cut off. */
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] *,
[data-testid="stMetricValue"], [data-testid="stMetricValue"] * {{
    white-space: normal !important; overflow: visible !important;
}}
[data-testid="stForm"], [class*="st-key-card_"] {{
    background-color: #ffffff; border: 1px solid {CARD_BORDER}; border-radius: 0.6rem;
}}
[class*="st-key-card_"] {{ padding: 0.75rem 1rem; }}
/* Title row: the title, the refresh button next to it, one blue line under both. */
.st-key-page_head {{
    border-bottom: 3px solid {SERIES_COLOR}; padding-bottom: 0.4rem; margin-bottom: 0.75rem;
}}
.st-key-page_head h1 {{ border-bottom: none; padding-bottom: 0; }}
.st-key-page_head [data-testid="stHeading"]:has(h1) {{ margin-bottom: 0; }}
.st-key-refresh_button button {{ color: {SERIES_COLOR}; }}
.st-key-refresh_button button [data-testid="stIconMaterial"] {{ font-size: 2rem; }}
/* The system name in the menu looks like a heading and acts like a button. */
.st-key-home_button button {{ padding: 0; text-align: left; justify-content: flex-start; }}
.st-key-home_button button p {{
    font-size: 1.45rem; font-weight: 700; line-height: 1.25; color: #ffffff; text-align: left;
}}
.st-key-home_button button:hover p {{ color: #6fb1ff; }}
/* The report picker is the first thing a reviewer uses, so its label stands out. */
.st-key-report_picker [data-testid="stWidgetLabel"] p {{
    font-size: 1.15rem; font-weight: 700; color: {NAVY};
}}
/* A field reacts only to a click inside its box. Without these rules the browser also
   selects a field on a click on its label, and puts the cursor into the date field (which
   opens the calendar) on a click in the empty space around it. */
[data-testid="stForm"], [data-testid="stForm"] [data-testid="stElementContainer"] {{
    user-select: none; -webkit-user-select: none;
}}
[data-testid="stForm"] input, [data-testid="stForm"] textarea,
[data-testid="stForm"] [data-testid="stDateInputField"] {{
    user-select: text; -webkit-user-select: text;
}}
[data-testid="stForm"] [data-testid="stWidgetLabel"] {{ pointer-events: none; }}
.report-box {{
    background-color: #ffffff; border: 1px solid {CARD_BORDER}; border-radius: 0.6rem;
    padding: 1rem; white-space: pre-wrap; overflow-wrap: anywhere;
    font-family: "Source Code Pro", ui-monospace, Menlo, monospace; font-size: 0.875rem;
    line-height: 1.6;
}}
.report-box strong {{ font-weight: 700; color: {NAVY}; }}
</style>
"""

settings = get_settings()


def open_db() -> sqlite3.Connection:
    conn = connect(settings.db_path)
    init_db(conn)
    return conn


# --- login -----------------------------------------------------------------------------


def _tile(
    column: DeltaGenerator, label: str, value: object, help: str | None = None, tone: str = "blue"
) -> None:
    """A number box. Every page uses this one, so the boxes look the same everywhere."""
    slug = "".join(c if c.isalnum() else "_" for c in label.lower())
    column.container(key=f"tile_{tone}_{slug}").metric(label, value, help=help)


def require_login() -> str:
    """Stops the page until a reviewer has logged in. Returns the reviewer's name."""
    if st.session_state.get("reviewer"):
        return str(st.session_state["reviewer"])
    st.title("Defect Report Automation")
    expected = settings.review_password.get_secret_value() if settings.review_password else ""
    if settings.review_login_open:
        st.warning("Demo mode: any password is accepted (REVIEW_LOGIN_OPEN is on).")
    elif not expected:
        st.error("No review password is set. Add REVIEW_PASSWORD to the .env file and restart.")
        st.stop()
    box, _ = st.columns(2)  # the page is wide; the login box takes the left half only
    with box.form("login"):
        name = st.text_input("Your name")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in", type="primary")
    if submitted:
        password_ok = settings.review_login_open or hmac.compare_digest(
            password.encode(), expected.encode()
        )
        if name.strip() and password_ok:
            st.session_state["reviewer"] = name.strip()
            st.rerun()
        if settings.review_login_open:
            st.error("Enter your name.")
        else:
            st.error("Enter your name and the correct password.")
    st.stop()


# --- review page -----------------------------------------------------------------------


def _label(item: ReviewItem, name: str, threshold: float) -> str:
    score = item.confidence.get(name)
    if score is None:
        return FIELD_LABELS[name]
    flag = " ⚠" if score < threshold else ""
    return f"{FIELD_LABELS[name]} · confidence {score:.2f}{flag}"


MAIL_HEADER = re.compile(r"^(From|To|Sent|Subject):", re.MULTILINE)
EMPTY_HEADER = re.compile(r"^(From|To|Sent|Subject):\s*$")


def _report_box(text: str) -> None:
    """Show a report's original text, with the mail header names in bold.

    The text comes from outside, so it is escaped first: nothing in a report can act as
    HTML. Only the header block (everything before the first empty line) gets bold names.
    A header line without a value is left out: a mailed-in report has no "To" address.
    """
    head, gap, body = html.escape(text, quote=False).partition("\n\n")
    head = "\n".join(line for line in head.split("\n") if not EMPTY_HEADER.match(line))
    head = MAIL_HEADER.sub(r"<strong>\1:</strong>", head)
    st.html(f'<div class="report-box">{head}{gap}{body}</div>')


def _text(value: str | None) -> str:
    """A missing field starts as an empty box, never as None."""
    return value or ""


def _clean(value: str | None) -> str | None:
    """An empty or blank box means the field is missing."""
    return (value or "").strip() or None


def _select(label: str, options: list[str], current: str | None, key: str) -> str | None:
    choices = ["", *options]
    if current and current not in choices:
        choices.append(current)  # keep an unexpected value visible instead of dropping it
    picked = st.selectbox(label, choices, index=choices.index(current or ""), key=key)
    return picked or None


def _field_form(item: ReviewItem, stations: list[str]) -> tuple[dict[str, object], str, str | None]:
    """Draw the editable fields. Returns (values, note, action clicked or None)."""
    f = item.fields
    k = f"r{item.report_id}_"
    t = settings.confidence_threshold
    with st.form(f"review_{item.report_id}"):
        left, right = st.columns(2)
        with left:
            order = st.text_input(
                _label(item, "order_number", t),
                _text(f.order_number if f else None),
                key=k + "order",
            )
            part = st.text_input(
                _label(item, "part_number", t), _text(f.part_number if f else None), key=k + "part"
            )
            supplier = st.text_input(
                _label(item, "supplier", t), _text(f.supplier if f else None), key=k + "supplier"
            )
            quantity = st.number_input(
                _label(item, "quantity_affected", t),
                min_value=1,
                step=1,
                value=f.quantity_affected if f else None,
                key=k + "quantity",
            )
            reporter = st.text_input(
                _label(item, "reporter", t), _text(f.reporter if f else None), key=k + "reporter"
            )
        with right:
            defect_type = _select(
                _label(item, "defect_type", t),
                [d.value for d in DefectType],
                f.defect_type if f else None,
                k + "type",
            )
            severity = _select(
                _label(item, "severity", t),
                [s.value for s in Severity],
                f.severity if f else None,
                k + "severity",
            )
            station = _select(
                _label(item, "detected_at", t),
                stations,
                f.detected_at if f else None,
                k + "station",
            )
            detected_on = st.date_input(
                _label(item, "detected_on", t),
                value=f.detected_on if f else None,
                max_value=date.today(),
                format="YYYY-MM-DD",
                key=k + "date",
            )
        description = st.text_area(
            _label(item, "defect_description", t),
            _text(f.defect_description if f else None),
            key=k + "description",
        )
        note = st.text_input("Note (optional, sent with the ticket)", key=k + "note")
        approve_col, reject_col = st.columns(2)  # equal width, so neither label is cut off
        approved = approve_col.form_submit_button(
            "Approve and post", type="primary", width="stretch", key="approve_button"
        )
        rejected = reject_col.form_submit_button(
            "Reject", type="primary", width="stretch", key="reject_button"
        )
    values: dict[str, object] = {
        "order_number": _clean(order),
        "part_number": _clean(part),
        "supplier": _clean(supplier),
        "defect_type": defect_type,
        "defect_description": _clean(description),
        "quantity_affected": quantity,
        "severity": severity,
        "detected_at": station,
        "detected_on": detected_on,
        "reporter": _clean(reporter),
    }
    return values, note, APPROVE if approved else REJECT if rejected else None


def _decide(
    item: ReviewItem,
    reviewer: str,
    action: str,
    fields: ExtractedFields | None,
    note: str,
) -> None:
    """Save the decision and, on approve, post the ticket. Runs after the reviewer confirmed."""
    run_id = new_run_id()
    configure_logging(settings.log_path, run_id)
    conn = open_db()  # a dialog reruns on its own thread, so it needs its own connection
    try:
        submit_review(conn, run_id, item.report_id, reviewer, action, fields, note or "")
    except ReviewError as exc:
        st.error(f"Not saved: {exc}")
        return

    if action == REJECT:
        st.session_state["flash"] = f"{item.file_name} rejected. No ticket was created."
    else:
        http = make_http_client(settings)
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (item.report_id,)).fetchone()
        ticket_id = (
            post_report(conn, http, settings, run_id, report) if qm_available(http) else None
        )
        if ticket_id:
            st.session_state["flash"] = f"{item.file_name} approved and posted as {ticket_id}."
        else:
            st.session_state["flash"] = (
                f"{item.file_name} approved, but the QM API did not take it. "
                "It posts on the next pipeline run."
            )
    st.rerun()  # closes the dialog and refreshes the queue


def _clear_pending() -> None:
    st.session_state.pop("pending", None)


@st.dialog("Please confirm", on_dismiss=_clear_pending)
def _confirm(
    item: ReviewItem,
    reviewer: str,
    action: str,
    fields: ExtractedFields | None,
    note: str,
) -> None:
    """Nothing is saved or posted until the reviewer says yes here."""
    if action == APPROVE:
        st.write(f"Are you sure you want to approve **{item.file_name}**?")
        if fields is not None and fields.severity == Severity.CRITICAL:
            st.warning("This is a critical defect.")
        st.caption("The ticket is posted to the QM system right away. This cannot be undone.")
        yes_label, yes_key = "Yes, approve and post", "confirm_approve_button"
    else:
        st.write(f"Are you sure you want to reject **{item.file_name}**?")
        st.caption("No ticket will be created. This cannot be undone.")
        yes_label, yes_key = "Yes, reject", "confirm_reject_button"
    yes, no = st.columns(2)
    if yes.button(yes_label, type="primary", width="stretch", key=yes_key):
        _clear_pending()
        _decide(item, reviewer, action, fields, note)
    if no.button("Cancel", width="stretch"):
        _clear_pending()
        st.rerun()


def _start_decision(
    item: ReviewItem, reviewer: str, action: str, values: dict[str, object], note: str
) -> None:
    """Check the form, then remember the decision until the reviewer answers the popup.

    The decision waits in the session, so the popup's answer is handled the same way
    whether Streamlit reruns only the popup or the whole page.
    """
    fields: ExtractedFields | None = None
    if action == APPROVE:
        try:
            fields = ExtractedFields.model_validate(values)
            check_approvable(fields)
        except ValidationError as exc:
            problems = "; ".join(f"{e['loc'][0]}: {e['msg']}" for e in exc.errors())
            st.error(f"Not saved. {problems}")
            return
        except ReviewError as exc:
            st.error(f"Not saved: {exc}")
            return
    st.session_state["pending"] = {
        "report_id": item.report_id,
        "action": action,
        "fields": fields,
        "note": note,
    }


def _page_title(title: str) -> None:
    """The page title with a refresh button next to it.

    The button reloads the page's data and keeps the login. The browser's own reload
    starts a new session, and a new session has to log in again.
    """
    with st.container(horizontal=True, vertical_alignment="center", key="page_head"):
        st.title(title, width="content")
        st.button(
            "",
            icon=":material/refresh:",
            help="Refresh this page. You stay logged in.",
            key="refresh_button",
            type="tertiary",
        )  # a click reruns the script, which reads everything from the database again


def review_page() -> None:
    reviewer = st.session_state["reviewer"]
    _page_title("Review queue")
    if flash := st.session_state.pop("flash", None):
        st.success(flash)

    conn = open_db()
    today = review_day(conn)
    queue = review_queue(conn)
    waiting_tile, arrived_tile, decided_tile, _ = st.columns(4)
    _tile(
        waiting_tile,
        "Total review reports waiting",
        len(queue),
        help="All reports that still need a decision. Critical ones come first in the list.",
        tone="amber",
    )
    _tile(
        arrived_tile,
        "Review reports arrived today",
        today.arrived,
        help="Reports that were sent to the review queue today.",
    )
    _tile(
        decided_tile,
        "Review decisions taken today",
        today.decisions,
        help=f"{today.approved} approved and {today.rejected} rejected today, "
        "all reviewers together.",
        tone="green",
    )

    if not queue:
        st.info("Nothing is waiting for review.")
        return
    ids = [row["id"] for row in queue]
    labels = {
        row["id"]: f"{row['file_name']} · {row['severity'] or 'no extraction'}" for row in queue
    }
    report_id = st.container(key="report_picker").selectbox(
        "Select a report to review", ids, format_func=lambda i: labels[i]
    )
    item = load_review_item(conn, report_id)

    st.subheader("Review findings")
    for reason in item.reasons or ["No reason recorded."]:
        st.warning(reason)

    original, extracted = st.columns(2)
    with original:
        st.subheader("Original report")
        _report_box(item.original_text)
    with extracted:
        st.subheader("Extracted fields")
        stations = [r[0] for r in conn.execute("SELECT station_code FROM master_stations")]
        values, note, action = _field_form(item, stations)
        if action:
            _start_decision(item, reviewer, action, values, note)
    pending = st.session_state.get("pending")
    if pending and pending["report_id"] == item.report_id:
        _confirm(item, reviewer, pending["action"], pending["fields"], pending["note"])
    elif pending:
        _clear_pending()  # the reviewer moved to another report: drop the stale decision


# --- rejected page ---------------------------------------------------------------------


def rejected_page() -> None:
    _page_title("Rejected")
    rejected = list_rejected(open_db())
    if not rejected:
        st.info("No reports have been rejected.")
        return
    total_tile, _ = st.columns([1, 3])
    _tile(
        total_tile,
        "Total reports rejected",
        len(rejected),
        help="Reports a reviewer rejected. No ticket was created for them.",
        tone="red",
    )
    st.caption("Newest first. This page is read-only.")
    table = pd.DataFrame(
        [
            {
                "Report": r.file_name,
                "Rejected (UTC)": r.rejected_at[:16].replace("T", " "),
                "Rejected by": r.reviewer,
                "Note": r.note,
            }
            for r in rejected
        ]
    )
    st.dataframe(table, hide_index=True, width="stretch")

    st.subheader("Report details")
    by_id = {r.report_id: r for r in rejected}
    report = by_id[st.selectbox("Report", list(by_id), format_func=lambda i: by_id[i].file_name)]
    original, why = st.columns(2)
    with original:
        st.markdown("**Original report**")
        _report_box(report.original_text)
    with why:
        st.markdown("**Review findings**")
        for reason in report.reasons or ["No reason recorded."]:
            st.warning(reason)
        st.markdown("**Reviewer's note**")
        st.write(report.note or "No note was left.")


# --- tickets page ----------------------------------------------------------------------


def tickets_page() -> None:
    _page_title("Tickets")
    tickets = list_tickets(open_db())
    if not tickets:
        st.info("No tickets have been posted yet.")
        return
    automatic = sum(1 for t in tickets if t.payload.approved_by == AUTO_APPROVER)
    total_tile, auto_tile, human_tile, _ = st.columns(4)
    _tile(
        total_tile,
        "Total tickets posted",
        len(tickets),
        help="Tickets that were sent to the QM system.",
        tone="green",
    )
    _tile(
        auto_tile,
        "Approved automatically",
        automatic,
        help="All checks passed and the defect was not critical, so no person was needed.",
    )
    _tile(
        human_tile,
        "Approved by a person",
        len(tickets) - automatic,
        help="A reviewer approved these in the review queue.",
    )
    st.caption("Newest first. This page is read-only: tickets cannot be changed or deleted here.")
    table = pd.DataFrame(
        [
            {
                "Ticket": t.ticket_id,
                "Posted (UTC)": t.posted_at[:16].replace("T", " "),
                "Report": t.file_name,
                "Order": t.payload.order_number,
                "Part": t.payload.part_number,
                "Supplier": t.payload.supplier,
                "Defect type": t.payload.defect_type,
                "Severity": t.payload.severity,
                "Qty": t.payload.quantity_affected,
                "Approved": "automatic"
                if t.payload.approved_by == AUTO_APPROVER
                else "human review",
            }
            for t in tickets
        ]
    )
    st.dataframe(table, hide_index=True, width="stretch")

    st.subheader("Ticket details")
    by_id = {t.ticket_id: t for t in tickets}
    ticket = by_id[
        st.selectbox("Ticket", list(by_id), format_func=lambda i: f"{i} · {by_id[i].file_name}")
    ]
    original, sent = st.columns(2)
    with original:
        st.markdown("**Original report**")
        _report_box(ticket.original_text)
    with sent:
        st.markdown("**Ticket as posted**")
        details = ticket.payload.model_dump(exclude={"source_report", "approved_by"})
        rows = [
            {"Field": FIELD_LABELS[name], "Value": "–" if value is None else str(value)}
            for name, value in details.items()
        ]
        # Tall enough for every row, so the last field is never hidden behind a scrollbar.
        st.dataframe(
            pd.DataFrame(rows), hide_index=True, width="stretch", height=35 * (len(rows) + 1) + 3
        )
        if ticket.note and ticket.payload.reviewer_note is None:
            # An older ticket: its note was written before notes were sent along.
            st.markdown("**Reviewer's note**")
            st.write(ticket.note)
            st.caption("This note is in the audit trail only. It was not sent with the ticket.")


# --- metrics page ----------------------------------------------------------------------


def _bar_chart(data: dict[str, int], what: str) -> None:
    """Horizontal bars, largest first, one colour: the bars show magnitude, not identity."""
    if not data:
        st.caption("No data yet.")
        return
    frame = pd.DataFrame({what: list(data), "Reports": list(data.values())})
    base = alt.Chart(frame).encode(
        y=alt.Y(f"{what}:N", sort="-x", title=None, axis=alt.Axis(labelLimit=260)),
        x=alt.X("Reports:Q", title=None, axis=alt.Axis(tickMinStep=1, grid=False, labels=False)),
        tooltip=[what, "Reports"],
    )
    bars = base.mark_bar(color=SERIES_COLOR, cornerRadiusEnd=4, height=16)
    numbers = base.mark_text(align="left", dx=5).encode(text="Reports:Q")
    chart = (bars + numbers).properties(height=max(90, 30 * len(frame)))
    chart = chart.configure(background="#ffffff")  # the chart sits on a white card
    with st.container(key=f"card_{what.lower().replace(' ', '_')}"):  # a white card
        st.altair_chart(chart, width="stretch")
        with st.expander("Show as table"):
            st.dataframe(frame, hide_index=True, width="stretch")


def _seconds(value: float | None) -> str:
    return "–" if value is None else f"{value:.1f} s"


def _accuracy_section() -> None:
    """Accuracy comes from the latest saved evaluation run, not from live reports:
    only the labelled reports have known right answers."""
    st.subheader("Extraction accuracy (evaluation set)")
    latest = settings.eval_results_dir / "latest.json"
    if not latest.exists():
        st.info("No evaluation run yet. Run: python -m defect_automation eval")
        return
    run = json.loads(latest.read_text(encoding="utf-8"))
    st.caption(
        f"{run['reports']} labelled reports · run {run['run_at'][:10]} · "
        f"model {run['model']} · prompt {run['prompt_version']}"
    )
    recall = run["critical_recall"]
    tiles = st.columns(4)
    _tile(tiles[0], "Field accuracy", f"{run['field_accuracy']:.1%}")
    _tile(
        tiles[1],
        "Critical recall",
        "–" if recall is None else f"{recall:.0%}",
        help=f"{run['critical_sent_to_review']} of {run['critical_total']} critical reports "
        "reached a person.",
        tone="green",
    )
    _tile(tiles[2], "Review rate (eval set)", f"{run['review_rate']:.0%}", tone="amber")
    _tile(
        tiles[3],
        "Auto-approved with an error",
        f"{run['auto_approved_with_errors']} of {run['auto_approved']}",
    )
    per_field = pd.DataFrame(
        [
            {
                "Field": name,
                "Correct": f"{int(s['correct'])}/{int(s['total'])}",
                "Accuracy": s["accuracy"],
            }
            for name, s in run["per_field"].items()
        ]
    )
    st.dataframe(
        per_field,
        hide_index=True,
        width="stretch",
        column_config={
            "Accuracy": st.column_config.ProgressColumn(
                "Accuracy", format="percent", min_value=0.0, max_value=1.0
            )
        },
    )


def metrics_page() -> None:
    _page_title("Metrics")
    m: Metrics = compute_metrics(open_db())
    st.caption("Counted per report and per defect. Nothing here is counted per person.")

    tiles = [*st.columns(3), *st.columns(3)]  # two rows of three, so no title is squeezed
    _tile(tiles[0], "Reports received", m.total_reports)
    _tile(tiles[1], "Tickets posted", m.tickets_posted, tone="green")
    _tile(
        tiles[2],
        "Review rate",
        "–" if m.review_rate is None else f"{m.review_rate:.0%}",
        help=f"{m.sent_to_review} of {m.decided_reports} routed reports went to a person.",
        tone="amber",
    )
    approved, rejected = m.human_decisions.get(APPROVE, 0), m.human_decisions.get(REJECT, 0)
    _tile(
        tiles[3],
        "Rejection rate after review",
        "–" if m.rejection_rate is None else f"{m.rejection_rate:.0%}",
        help=f"{rejected} of {approved + rejected} reports that a person decided on were "
        "rejected. Reports still waiting in the queue are not counted.",
        tone="red",
    )
    _tile(
        tiles[4],
        "Median processing time",
        _seconds(m.median_processing_seconds),
        help="Per report, from the start of processing to the routing decision. "
        "Waiting in the queue and human review are not included. "
        f"Slowest: {_seconds(m.max_processing_seconds)}.",
    )
    _tile(
        tiles[5],
        "Model cost per report",
        "–" if m.cost_per_report_eur is None else f"{m.cost_per_report_eur:.4f} €",
        help=f"Total: {m.cost_total_eur:.4f} €. Priced as if on a paid key.",
    )

    left, right = st.columns(2)
    with left:
        st.subheader("Reports by status")
        _bar_chart(m.by_status, "Status")
        st.subheader("Why reports went to review")
        _bar_chart(m.review_reasons, "Reason")
    with right:
        st.subheader("Defect types")
        _bar_chart(m.defect_types, "Defect type")
        st.subheader("Severity")
        _bar_chart(m.severities, "Severity")

    _accuracy_section()

    st.subheader("Review decisions")
    st.write(f"{approved} approved and {rejected} rejected by a person, all reviewers together.")


# --- app -------------------------------------------------------------------------------

st.set_page_config(page_title="Defect Report Automation", layout="wide")
st.html(APP_CSS)
reviewer_name = require_login()
with st.sidebar:
    # The system name works like a site logo: a click refreshes the page, login kept.
    st.button(
        "Defect Report Review System",
        key="home_button",
        type="tertiary",
        help="Refresh this page. You stay logged in.",
    )
    st.divider()
    st.write(f"Logged in as **{reviewer_name}**")
    page = st.radio("Page", ["Review queue", "Tickets", "Rejected", "Metrics"])
    if st.button("Log out"):
        del st.session_state["reviewer"]
        st.rerun()
if page == "Metrics":
    metrics_page()
elif page == "Tickets":
    tickets_page()
elif page == "Rejected":
    rejected_page()
else:
    review_page()
