"""Review screen and metrics page.

Run: streamlit run streamlit_app.py
"""

import hmac
import json
import sqlite3
from datetime import date

import altair as alt
import pandas as pd
import streamlit as st
from pydantic import ValidationError

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
    load_review_item,
    review_queue,
    submit_review,
)
from defect_automation.schemas import ExtractedFields

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
}

# Approve is green and Reject is red, on the form and in the confirmation popup.
# Streamlit gives a widget with key "x" the CSS class "st-key-x". The labels still say
# what each button does, so the meaning never depends on colour alone.
APPROVE_GREEN, APPROVE_GREEN_HOVER = "#1a7f37", "#156a2d"
REJECT_RED, REJECT_RED_HOVER = "#c62828", "#a82020"
DECISION_BUTTON_CSS = f"""
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
</style>
"""

settings = get_settings()


def open_db() -> sqlite3.Connection:
    conn = connect(settings.db_path)
    init_db(conn)
    return conn


# --- login -----------------------------------------------------------------------------


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
    with st.form("login"):
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
        note = st.text_input("Note (optional, stored in the audit trail)", key=k + "note")
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


def review_page() -> None:
    reviewer = st.session_state["reviewer"]
    st.html(DECISION_BUTTON_CSS)
    st.title("Review queue")
    if flash := st.session_state.pop("flash", None):
        st.success(flash)

    conn = open_db()
    queue = review_queue(conn)
    if not queue:
        st.info("Nothing is waiting for review.")
        return
    st.caption(f"{len(queue)} reports waiting. Critical ones come first.")
    ids = [row["id"] for row in queue]
    labels = {
        row["id"]: f"{row['file_name']} · {row['severity'] or 'no extraction'}" for row in queue
    }
    report_id = st.selectbox("Report", ids, format_func=lambda i: labels[i])
    item = load_review_item(conn, report_id)

    st.subheader("Why this needs a person")
    for reason in item.reasons or ["No reason recorded."]:
        st.warning(reason)

    original, extracted = st.columns(2)
    with original:
        st.subheader("Original report")
        st.code(item.original_text, language=None, wrap_lines=True)
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


# --- tickets page ----------------------------------------------------------------------


def tickets_page() -> None:
    st.title("Tickets")
    tickets = list_tickets(open_db())
    if not tickets:
        st.info("No tickets have been posted yet.")
        return
    st.caption(
        f"{len(tickets)} tickets posted to the QM system, newest first. "
        "This page is read-only: tickets cannot be changed or deleted here."
    )
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
        st.code(ticket.original_text, language=None, wrap_lines=True)
    with sent:
        st.markdown("**Ticket as posted**")
        details = ticket.payload.model_dump(exclude={"source_report", "approved_by"})
        rows = [
            {"Field": FIELD_LABELS[name], "Value": "–" if value is None else str(value)}
            for name, value in details.items()
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


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
    tiles[0].metric("Field accuracy", f"{run['field_accuracy']:.1%}")
    tiles[1].metric(
        "Critical recall",
        "–" if recall is None else f"{recall:.0%}",
        help=f"{run['critical_sent_to_review']} of {run['critical_total']} critical reports "
        "reached a person.",
    )
    tiles[2].metric("Review rate (eval set)", f"{run['review_rate']:.0%}")
    tiles[3].metric(
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
    st.title("Metrics")
    m: Metrics = compute_metrics(open_db())
    st.caption("Counted per report and per defect. Nothing here is counted per person.")

    tiles = st.columns(5)
    tiles[0].metric("Reports received", m.total_reports)
    tiles[1].metric("Tickets posted", m.tickets_posted)
    tiles[2].metric(
        "Review rate",
        "–" if m.review_rate is None else f"{m.review_rate:.0%}",
        help=f"{m.sent_to_review} of {m.decided_reports} routed reports went to a person.",
    )
    tiles[3].metric(
        "Median processing time",
        _seconds(m.median_processing_seconds),
        help="Per report, from the start of processing to the routing decision. "
        "Waiting in the queue and human review are not included. "
        f"Slowest: {_seconds(m.max_processing_seconds)}.",
    )
    tiles[4].metric(
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
    approved, rejected = m.human_decisions.get(APPROVE, 0), m.human_decisions.get(REJECT, 0)
    st.write(f"{approved} approved and {rejected} rejected by a person, all reviewers together.")


# --- app -------------------------------------------------------------------------------

st.set_page_config(page_title="Defect Report Automation", layout="wide")
reviewer_name = require_login()
with st.sidebar:
    st.write(f"Logged in as **{reviewer_name}**")
    page = st.radio("Page", ["Review queue", "Tickets", "Metrics"])
    if st.button("Log out"):
        del st.session_state["reviewer"]
        st.rerun()
if page == "Metrics":
    metrics_page()
elif page == "Tickets":
    tickets_page()
else:
    review_page()
