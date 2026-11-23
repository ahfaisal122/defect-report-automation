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

# --- design system ---------------------------------------------------------------------
# One look on every page, in the style of a modern internal tool: a light canvas, white
# cards with soft borders, a dark slate menu, one accent blue. Colour carries meaning and
# is the same everywhere: blue = information, amber = waiting for a person, green = done,
# red = rejected. A label or icon always says the same thing, so colour is never alone.
SERIES_COLOR = "#2563eb"  # the accent: charts here, buttons and links via config.toml
INK, MUTED, BORDER, CANVAS = "#0f172a", "#64748b", "#e2e8f0", "#f5f7fb"
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

# Number boxes: (soft tint, strong colour, Material icon in the corner).
TILE_TONES = {
    "blue": ("#eff6ff", "#2563eb", "insights"),
    "amber": ("#fffbeb", "#d97706", "hourglass_top"),
    "green": ("#ecfdf5", "#059669", "task_alt"),
    "red": ("#fef2f2", "#dc2626", "block"),
}
TILE_CSS = "\n".join(
    f'[class*="st-key-tile_{tone}_"] {{ --tint: {tint}; --tone: {strong}; --icon: "{icon}"; }}'
    for tone, (tint, strong, icon) in TILE_TONES.items()
)
SEVERITY_TONES = {"critical": "red", "major": "amber", "minor": "green"}
APPROVE_GREEN, APPROVE_GREEN_HOVER = "#059669", "#047857"
REJECT_RED, REJECT_RED_HOVER = "#dc2626", "#b91c1c"

APP_CSS = f"""
<style>
/* ---------- canvas and type ---------- */
[data-testid="stAppViewContainer"] {{ background: {CANVAS}; }}
[data-testid="stHeader"] {{ background: transparent; }}
[data-testid="stMainBlockContainer"] {{
    padding-top: 2.25rem; padding-bottom: 4rem; max-width: 1440px;
}}
h1, h2, h3 {{ color: {INK}; letter-spacing: -0.015em; }}
h1 {{ font-size: 1.75rem !important; padding: 0 !important; }}
h3 {{ font-size: 1.05rem !important; padding: 0.25rem 0 0.15rem !important; }}
[data-testid="stCaptionContainer"] p {{ color: {MUTED}; }}

/* ---------- page header: title, refresh button, one line that says what the page is ---------- */
.st-key-page_head {{
    border-bottom: 1px solid {BORDER}; padding-bottom: 1rem; margin-bottom: 0.5rem; gap: 0.2rem;
}}
.st-key-page_head [data-testid="stHeading"] {{ margin: 0; }}
.st-key-page_head [data-testid="stCaptionContainer"] p {{ font-size: 0.92rem; margin: 0; }}
.st-key-refresh_button button {{
    width: 2.25rem; height: 2.25rem; min-height: 0; padding: 0; border-radius: 0.6rem;
    border: 1px solid {BORDER}; background: #ffffff; color: {MUTED};
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
}}
.st-key-refresh_button button:hover {{ color: {SERIES_COLOR}; border-color: #bfdbfe; }}
.st-key-refresh_button [data-testid="stIconMaterial"] {{ font-size: 1.2rem; }}

/* ---------- cards ---------- */
[data-testid="stForm"], [class*="st-key-card_"], .report-box, .case-head {{
    background: #ffffff; border: 1px solid {BORDER}; border-radius: 0.9rem;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 2px 8px rgba(15, 23, 42, 0.03);
}}
[data-testid="stForm"] {{ padding: 1.25rem 1.35rem; }}
[class*="st-key-card_"] {{ padding: 1rem 1.15rem; }}
[class*="st-key-card_"] [data-testid="stDataFrame"] {{ border-radius: 0.6rem; }}

/* ---------- number boxes ---------- */
{TILE_CSS}
[class*="st-key-tile_"] {{ height: 100%; }}
[class*="st-key-tile_"] [data-testid="stMetric"] {{
    height: 100%; background: #ffffff; border: 1px solid {BORDER}; border-radius: 0.9rem;
    padding: 1.05rem 1.2rem 1rem;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 2px 8px rgba(15, 23, 42, 0.03);
}}
[class*="st-key-tile_"] [data-testid="stMetric"]::before {{
    content: var(--icon); font-family: "Material Symbols Rounded"; font-size: 1.25rem;
    width: 2.2rem; height: 2.2rem; margin-bottom: 0.7rem;
    display: flex; align-items: center; justify-content: center;
    border-radius: 0.6rem; background: var(--tint); color: var(--tone);
}}
[data-testid="stMetricLabel"] p {{ color: {MUTED}; font-size: 0.86rem; font-weight: 500; }}
[data-testid="stMetricValue"] {{
    color: {INK}; font-size: 1.9rem; font-weight: 700; letter-spacing: -0.02em;
}}
/* Long box titles wrap onto a second line; they are never cut off. */
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] *,
[data-testid="stMetricValue"], [data-testid="stMetricValue"] * {{
    white-space: normal !important; overflow: visible !important;
}}

/* ---------- sidebar ---------- */
[data-testid="stSidebar"] {{ border-right: 1px solid #1e293b; }}
[data-testid="stSidebarHeader"] {{ height: 1rem; }}
[data-testid="stSidebar"] hr {{ border-color: #1e293b; margin: 1rem 0; }}
.nav-label {{
    font-size: 0.7rem; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
    color: #64748b; margin: 0.25rem 0 -0.25rem 0.75rem;
}}
/* The system name works like a site logo: a logo mark, then the name. */
.st-key-home_button button {{
    display: flex; align-items: center; justify-content: flex-start; gap: 0.75rem;
    width: 100%; padding: 0; text-align: left;
}}
.st-key-home_button button::before {{
    content: "verified"; font-family: "Material Symbols Rounded"; font-size: 1.35rem;
    flex: none; width: 2.5rem; height: 2.5rem; border-radius: 0.7rem;
    display: flex; align-items: center; justify-content: center; color: #ffffff;
    background: linear-gradient(135deg, #3b82f6, #6366f1);
    box-shadow: 0 4px 12px rgba(59, 130, 246, 0.35);
}}
.st-key-home_button button p {{
    font-size: 1rem; font-weight: 650; line-height: 1.25; color: #ffffff; text-align: left;
}}
.st-key-home_button button:hover p {{ color: #bfdbfe; }}
/* Menu: full-width items with an icon, no radio circles. */
.st-key-nav, .st-key-nav [data-testid="stRadio"] {{ width: 100% !important; }}
[data-testid="stSidebar"] [data-testid="stRadioGroup"] {{
    gap: 0.1rem; width: 100%; align-items: stretch;
}}
[data-testid="stSidebar"] [data-testid="stRadioGroup"] > div {{ width: 100%; }}
[data-testid="stSidebar"] [data-testid="stRadioOption"] {{
    display: flex !important; width: 100% !important; max-width: none !important;
    margin: 0; padding: 0.55rem 0.75rem; border-radius: 0.6rem;
    transition: background 0.15s ease; cursor: pointer;
}}
[data-testid="stSidebar"] [data-testid="stRadioOption"] > div {{ flex: 1; padding: 0; }}
[data-testid="stSidebar"] [data-testid="stRadioOption"] > div > div:first-child {{
    display: none;
}}
[data-testid="stSidebar"] [data-testid="stRadioOption"] [data-testid="stMarkdownContainer"] {{
    flex: 1;
}}
[data-testid="stSidebar"] [data-testid="stRadioOption"] p {{
    display: flex; align-items: center; width: 100%;
    color: #cbd5e1; font-size: 0.93rem; font-weight: 500;
}}
[data-testid="stSidebar"] [data-testid="stRadioOption"]:hover {{ background: #1e293b; }}
[data-testid="stSidebar"] [data-testid="stRadioOption"]:has(input:checked) {{
    background: rgba(59, 130, 246, 0.18); box-shadow: inset 3px 0 0 #60a5fa;
}}
[data-testid="stSidebar"] [data-testid="stRadioOption"]:has(input:checked) p {{
    color: #ffffff; font-weight: 600;
}}
/* Signed-in user. */
.user-card {{
    display: flex; align-items: center; gap: 0.7rem; padding: 0.7rem 0.75rem;
    border: 1px solid #1e293b; border-radius: 0.75rem; background: #111c33;
}}
.user-card .avatar {{
    flex: none; width: 2.2rem; height: 2.2rem; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    background: linear-gradient(135deg, #0ea5e9, #6366f1); color: #ffffff;
    font-weight: 650; font-size: 0.85rem;
}}
.user-card .name {{ color: #f8fafc; font-weight: 600; font-size: 0.92rem; line-height: 1.2; }}
.user-card .role {{ color: #94a3b8; font-size: 0.78rem; }}
.st-key-logout_button button {{ color: #94a3b8; padding-left: 0.75rem; }}
.st-key-logout_button button:hover {{ color: #ffffff; }}

/* ---------- review: the case header ---------- */
.case-head {{ padding: 1.1rem 1.3rem; display: flex; flex-direction: column; gap: 0.55rem; }}
.case-head .eyebrow {{
    font-size: 0.72rem; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
    color: {MUTED};
}}
.case-head .subject {{ font-size: 1.2rem; font-weight: 650; color: {INK}; line-height: 1.35; }}
.case-head .meta {{
    display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; color: {MUTED};
    font-size: 0.85rem;
}}
.pill {{
    display: inline-flex; align-items: center; gap: 0.3rem; padding: 0.15rem 0.6rem;
    border-radius: 999px; font-size: 0.78rem; font-weight: 600; border: 1px solid transparent;
}}
.pill.red {{ background: #fef2f2; color: #b91c1c; border-color: #fecaca; }}
.pill.amber {{ background: #fffbeb; color: #b45309; border-color: #fde68a; }}
.pill.green {{ background: #ecfdf5; color: #047857; border-color: #a7f3d0; }}
.pill.grey {{ background: #f1f5f9; color: #475569; border-color: {BORDER}; }}
.pill .dot {{ width: 0.45rem; height: 0.45rem; border-radius: 50%; background: currentColor; }}
.st-key-card_findings [data-testid="stAlertContainer"] {{ padding: 0.6rem 0.9rem; }}
.st-key-report_picker [data-testid="stWidgetLabel"] p {{
    font-size: 0.9rem; font-weight: 600; color: {INK};
}}

/* ---------- the original report, shown like an email ---------- */
.report-box {{ overflow: hidden; }}
.report-box .mail-head {{
    background: #f8fafc; border-bottom: 1px solid {BORDER}; padding: 0.85rem 1.1rem;
    white-space: pre-wrap; overflow-wrap: anywhere; font-size: 0.86rem; line-height: 1.7;
    color: #334155;
}}
.report-box .mail-head strong {{ font-weight: 600; color: {INK}; }}
.report-box .mail-body {{
    padding: 1rem 1.1rem 1.15rem; white-space: pre-wrap; overflow-wrap: anywhere;
    font-size: 0.93rem; line-height: 1.65; color: #1e293b;
}}

/* ---------- buttons: approve is green, reject is red, on the form and in the popup ---------- */
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
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {{
    font-weight: 600; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
}}

/* ---------- login ---------- */
.login-brand {{ text-align: center; margin: 9vh 0 1.5rem; }}
.login-brand .mark {{
    width: 3.4rem; height: 3.4rem; margin: 0 auto 1rem; border-radius: 1rem;
    display: flex; align-items: center; justify-content: center;
    background: linear-gradient(135deg, #3b82f6, #6366f1); color: #ffffff;
    font-family: "Material Symbols Rounded"; font-size: 1.9rem;
    box-shadow: 0 8px 24px rgba(59, 130, 246, 0.35);
}}
.login-brand .title {{
    font-size: 1.5rem; font-weight: 700; color: {INK}; letter-spacing: -0.02em;
}}
.login-brand .sub {{ color: {MUTED}; margin-top: 0.3rem; font-size: 0.95rem; }}
.login-foot {{ text-align: center; color: #94a3b8; font-size: 0.8rem; margin-top: 1.2rem; }}

/* ---------- a field reacts only to a click inside its box ----------
   Without these rules the browser also selects a field on a click on its label, and puts
   the cursor into the date field (which opens the calendar) on a click in the empty space
   around it. */
[data-testid="stForm"], [data-testid="stForm"] [data-testid="stElementContainer"] {{
    user-select: none; -webkit-user-select: none;
}}
[data-testid="stForm"] input, [data-testid="stForm"] textarea,
[data-testid="stForm"] [data-testid="stDateInputField"] {{
    user-select: text; -webkit-user-select: text;
}}
[data-testid="stForm"] [data-testid="stWidgetLabel"] {{ pointer-events: none; }}
[data-testid="stForm"] [data-testid="stWidgetLabel"] p {{
    font-size: 0.84rem; color: #334155; font-weight: 500;
}}
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
    expected = settings.review_password.get_secret_value() if settings.review_password else ""
    _, box, _ = st.columns([1, 1.05, 1])  # a centred sign-in card
    box.html(
        '<div class="login-brand"><div class="mark">verified</div>'
        '<div class="title">Defect Report Review System</div>'
        '<div class="sub">Sign in to review incoming quality reports</div></div>'
    )
    if settings.review_login_open:
        box.warning("Demo mode: any password is accepted (REVIEW_LOGIN_OPEN is on).")
    elif not expected:
        box.error("No review password is set. Add REVIEW_PASSWORD to the .env file and restart.")
        st.stop()
    with box.form("login"):
        name = st.text_input("Your name", placeholder="e.g. Anna Keller")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button(
            "Log in", type="primary", width="stretch", icon=":material/login:"
        )
    box.html('<div class="login-foot">Internal tool · Quality Management</div>')
    if submitted:
        password_ok = settings.review_login_open or hmac.compare_digest(
            password.encode(), expected.encode()
        )
        if name.strip() and password_ok:
            st.session_state["reviewer"] = name.strip()
            st.rerun()
        if settings.review_login_open:
            box.error("Enter your name.")
        else:
            box.error("Enter your name and the correct password.")
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
    escaped = html.escape(text, quote=False)
    head, _, body = escaped.partition("\n\n")
    if not MAIL_HEADER.search(head):  # no mail headers: the whole text is the body
        head, body = "", escaped
    head = "\n".join(line for line in head.split("\n") if not EMPTY_HEADER.match(line))
    head = MAIL_HEADER.sub(r"<strong>\1:</strong>", head)
    top = f'<div class="mail-head">{head}</div>' if head else ""
    st.html(
        f'<div class="report-box">{top}<div class="mail-body">{body.strip(chr(10))}</div></div>'
    )


SUBJECT = re.compile(r"^Subject:[ \t]*(.+)$", re.MULTILINE)


def _subject(text: str) -> str:
    """The email's subject line, or an empty text when the report has none."""
    match = SUBJECT.search(text)
    return match.group(1).strip() if match else ""


def _pill(text: str, tone: str) -> str:
    return f'<span class="pill {tone}"><span class="dot"></span>{html.escape(text)}</span>'


def _severity_pill(severity: str | None) -> str:
    if not severity:
        return _pill("No severity", "grey")
    return _pill(severity.capitalize(), SEVERITY_TONES.get(severity, "grey"))


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
            "Approve and post",
            type="primary",
            width="stretch",
            key="approve_button",
            icon=":material/check:",
        )
        rejected = reject_col.form_submit_button(
            "Reject", type="primary", width="stretch", key="reject_button", icon=":material/close:"
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


def _page_title(title: str, subtitle: str) -> None:
    """The page title with a refresh button next to it, and one line under it.

    The button reloads the page's data and keeps the login. The browser's own reload
    starts a new session, and a new session has to log in again.
    """
    with st.container(key="page_head"):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.title(title, width="content", anchor=False)
            st.button(
                "",
                icon=":material/refresh:",
                help="Refresh this page. You stay logged in.",
                key="refresh_button",
                type="tertiary",
            )  # a click reruns the script, which reads everything from the database again
        st.caption(subtitle)


def review_page() -> None:
    reviewer = st.session_state["reviewer"]
    _page_title("Review queue", "Reports the system could not approve on its own.")
    if flash := st.session_state.pop("flash", None):
        st.success(flash, icon=":material/check_circle:")

    conn = open_db()
    today = review_day(conn)
    queue = review_queue(conn)
    waiting_tile, arrived_tile, decided_tile = st.columns(3)
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
        st.info("Nothing is waiting for review.", icon=":material/inbox:")
        return
    ids = [row["id"] for row in queue]
    raw = dict(
        conn.execute(
            f"SELECT id, raw_text FROM reports WHERE id IN ({','.join('?' * len(ids))})", ids
        ).fetchall()
    )
    labels = {
        row["id"]: f"{_subject(raw.get(row['id'], '')) or row['file_name']}"
        f" · {row['severity'] or 'no extraction'}"
        for row in queue
    }
    report_id = st.container(key="report_picker").selectbox(
        "Select a report to review", ids, format_func=lambda i: labels[i]
    )
    item = load_review_item(conn, report_id)

    severity = item.fields.severity if item.fields else None
    count = len(item.reasons)
    st.html(
        '<div class="case-head"><div class="eyebrow">Report under review</div>'
        f'<div class="subject">{html.escape(_subject(item.original_text) or item.file_name)}'
        f'</div><div class="meta">{_severity_pill(severity)}'
        f"{_pill(f'{count} finding' + ('' if count == 1 else 's'), 'grey')}"
        f"<span>{html.escape(item.file_name)}</span>"
        f"<span>· received {item.received_at[:16].replace('T', ' ')} UTC</span></div></div>"
    )

    with st.container(key="card_findings"):
        st.subheader("Review findings", anchor=False)
        for reason in item.reasons or ["No reason recorded."]:
            st.warning(reason, icon=":material/flag:")

    original, extracted = st.columns(2, gap="medium")
    with original:
        st.subheader("Original report", anchor=False)
        _report_box(item.original_text)
    with extracted:
        st.subheader("Extracted fields", anchor=False)
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
    _page_title("Rejected", "Reports a reviewer turned down. No ticket was created for them.")
    rejected = list_rejected(open_db())
    if not rejected:
        st.info("No reports have been rejected.", icon=":material/inbox:")
        return
    total_tile, _ = st.columns([1, 3])
    _tile(
        total_tile,
        "Total reports rejected",
        len(rejected),
        help="Reports a reviewer rejected. No ticket was created for them.",
        tone="red",
    )
    st.subheader("All rejected reports", anchor=False)
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
    with st.container(key="card_rejected_table"):
        st.dataframe(table, hide_index=True, width="stretch")

    st.subheader("Report details", anchor=False)
    by_id = {r.report_id: r for r in rejected}
    report = by_id[
        st.selectbox(
            "Report",
            list(by_id),
            format_func=lambda i: (
                f"{_subject(by_id[i].original_text) or by_id[i].file_name} · {by_id[i].file_name}"
            ),
        )
    ]
    original, why = st.columns(2, gap="medium")
    with original:
        st.markdown("**Original report**")
        _report_box(report.original_text)
    with why, st.container(key="card_rejected_why"):
        st.markdown("**Review findings**")
        for reason in report.reasons or ["No reason recorded."]:
            st.warning(reason, icon=":material/flag:")
        st.markdown("**Reviewer's note**")
        st.write(report.note or "No note was left.")
        st.caption(
            f"Rejected by {report.reviewer} · {report.rejected_at[:16].replace('T', ' ')} UTC"
        )


# --- tickets page ----------------------------------------------------------------------


def tickets_page() -> None:
    _page_title("Tickets", "Quality tickets sent to the QM system. Read-only.")
    tickets = list_tickets(open_db())
    if not tickets:
        st.info("No tickets have been posted yet.", icon=":material/inbox:")
        return
    automatic = sum(1 for t in tickets if t.payload.approved_by == AUTO_APPROVER)
    total_tile, auto_tile, human_tile = st.columns(3)
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
    st.subheader("All tickets", anchor=False)
    st.caption("Newest first. Tickets cannot be changed or deleted here.")
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
    with st.container(key="card_tickets_table"):
        st.dataframe(table, hide_index=True, width="stretch")

    st.subheader("Ticket details", anchor=False)
    by_id = {t.ticket_id: t for t in tickets}
    ticket = by_id[
        st.selectbox("Ticket", list(by_id), format_func=lambda i: f"{i} · {by_id[i].file_name}")
    ]
    original, sent = st.columns(2, gap="medium")
    with original:
        st.markdown("**Original report**")
        _report_box(ticket.original_text)
    with sent:
        st.markdown("**Ticket as posted**")
        details = ticket.payload.model_dump(exclude={"source_report", "approved_by"})
        if ticket.note and ticket.payload.reviewer_note is None:
            # A ticket from before notes were sent along: show the note, and say so.
            details["reviewer_note"] = f"{ticket.note} (not sent with this ticket)"
        rows = [
            {"Field": FIELD_LABELS[name], "Value": "–" if value is None else str(value)}
            for name, value in details.items()
        ]
        # Tall enough for every row, so the last field is never hidden behind a scrollbar.
        st.dataframe(
            pd.DataFrame(rows), hide_index=True, width="stretch", height=35 * (len(rows) + 1) + 3
        )


# --- metrics page ----------------------------------------------------------------------


def _bar_chart(data: dict[str, int], what: str, title: str) -> None:
    """A card with a title and horizontal bars, largest first, in one colour: the bars
    show magnitude, not identity."""
    if not data:
        with st.container(key=f"card_{what.lower().replace(' ', '_')}"):
            st.subheader(title, anchor=False)
            st.caption("No data yet.")
        return
    frame = pd.DataFrame({what: list(data), "Reports": list(data.values())})
    base = alt.Chart(frame).encode(
        y=alt.Y(f"{what}:N", sort="-x", title=None, axis=alt.Axis(labelLimit=260)),
        x=alt.X("Reports:Q", title=None, axis=alt.Axis(tickMinStep=1, grid=False, labels=False)),
        tooltip=[what, "Reports"],
    )
    bars = base.mark_bar(color=SERIES_COLOR, cornerRadiusEnd=4, height=16)
    numbers = base.mark_text(align="left", dx=6, color=INK, fontWeight=600).encode(text="Reports:Q")
    chart = (bars + numbers).properties(height=max(90, 30 * len(frame)))
    chart = (
        chart.configure(background="#ffffff", font="Inter")
        .configure_axis(labelColor=MUTED, labelFontSize=12, domain=False, ticks=False)
        .configure_view(strokeWidth=0)
    )  # the chart sits on a white card
    with st.container(key=f"card_{what.lower().replace(' ', '_')}"):  # a white card
        st.subheader(title, anchor=False)
        st.altair_chart(chart, width="stretch")
        with st.expander("Show as table"):
            st.dataframe(frame, hide_index=True, width="stretch")


def _seconds(value: float | None) -> str:
    return "–" if value is None else f"{value:.1f} s"


def _accuracy_section() -> None:
    """Accuracy comes from the latest saved evaluation run, not from live reports:
    only the labelled reports have known right answers."""
    st.subheader("Extraction accuracy (evaluation set)", anchor=False)
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
    st.container(key="card_per_field").dataframe(
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
    _page_title("Metrics", "Counted per report and per defect. Nothing here is counted per person.")
    m: Metrics = compute_metrics(open_db())

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

    left, right = st.columns(2, gap="medium")
    with left:
        _bar_chart(m.by_status, "Status", "Reports by status")
        _bar_chart(m.review_reasons, "Reason", "Why reports went to review")
    with right:
        _bar_chart(m.defect_types, "Defect type", "Defect types")
        _bar_chart(m.severities, "Severity", "Severity")
        with st.container(key="card_decisions"):
            st.subheader("Review decisions", anchor=False)
            st.write(
                f"{approved} approved and {rejected} rejected by a person, all reviewers together."
            )

    _accuracy_section()


# --- app -------------------------------------------------------------------------------

PAGES = {
    "Review queue": ":material/inbox:",
    "Tickets": ":material/confirmation_number:",
    "Rejected": ":material/block:",
    "Metrics": ":material/monitoring:",
}


def _initials(name: str) -> str:
    parts = name.split()
    return "".join(p[0] for p in parts[:2]).upper() or "?"


st.set_page_config(
    page_title="Defect Report Review System", page_icon=":material/verified:", layout="wide"
)
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
    st.html('<div class="nav-label">Menu</div>')
    page = st.radio(
        "Page",
        list(PAGES),
        format_func=lambda name: f"{PAGES[name]}&nbsp;&nbsp;{name}",
        label_visibility="collapsed",
        key="nav",
    )
    # The live count sits in a style rule, not in the menu labels: a label that changes
    # would make Streamlit treat the menu as new and jump back to the first page.
    waiting = len(review_queue(open_db()))
    if waiting:
        st.html(
            '<style>.st-key-nav [data-testid="stRadioGroup"] > div:first-child p::after {'
            f' content: "{waiting}"; margin-left: auto; padding: 0.05rem 0.5rem;'
            " border-radius: 999px; background: #f59e0b; color: #1f1300;"
            " font-size: 0.75rem; font-weight: 700; }</style>"
        )
    st.divider()
    st.html(
        f'<div class="user-card"><div class="avatar">{html.escape(_initials(reviewer_name))}</div>'
        f'<div><div class="name">{html.escape(reviewer_name)}</div>'
        '<div class="role">Reviewer</div></div></div>'
    )
    if st.button("Log out", key="logout_button", type="tertiary", icon=":material/logout:"):
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
