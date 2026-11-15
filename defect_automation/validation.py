"""Rule checks on extracted fields (FR-05). Pure functions: no model, no database writes.

Every check returns a result whether it passes or fails, so the audit trail shows what
was checked, not only what went wrong.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import date

from defect_automation.domain import CRITICAL_LANGUAGE, PART_NUMBER_PATTERN
from defect_automation.schemas import FIELD_NAMES, ExtractedFields


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    message: str = ""


def _normalise(name: str) -> str:
    """Compare supplier names without case, spacing or punctuation differences."""
    return re.sub(r"[^a-z0-9]", "", name.casefold())


@dataclass(frozen=True)
class MasterData:
    orders: frozenset[str]
    part_suppliers: dict[str, str]  # part number -> normalised supplier name
    suppliers: frozenset[str]  # normalised names
    stations: frozenset[str]

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "MasterData":
        parts = conn.execute(
            "SELECT p.part_number, s.name FROM master_parts p "
            "JOIN master_suppliers s ON s.supplier_id = p.supplier_id"
        )
        return cls(
            orders=frozenset(r[0] for r in conn.execute("SELECT order_number FROM master_orders")),
            part_suppliers={row[0]: _normalise(row[1]) for row in parts},
            suppliers=frozenset(
                _normalise(r[0]) for r in conn.execute("SELECT name FROM master_suppliers")
            ),
            stations=frozenset(
                r[0] for r in conn.execute("SELECT station_code FROM master_stations")
            ),
        )


def _exists(name: str, label: str, value: str | None, known: bool) -> CheckResult:
    if value is None:
        return CheckResult(name, False, f"no {label} in the report")
    if not known:
        return CheckResult(name, False, f"{label} {value!r} not found in master data")
    return CheckResult(name, True)


def check_required_fields(fields: ExtractedFields, required: tuple[str, ...]) -> CheckResult:
    values = fields.model_dump()
    missing = [name for name in required if values[name] in (None, "")]
    if missing:
        return CheckResult("required_fields", False, f"missing: {', '.join(missing)}")
    return CheckResult("required_fields", True)


def check_supplier_matches_part(fields: ExtractedFields, master: MasterData) -> CheckResult:
    name = "supplier_matches_part"
    expected = master.part_suppliers.get(fields.part_number or "")
    if expected is None or fields.supplier is None:
        return CheckResult(name, True, "not checked: part or supplier unknown")
    if _normalise(fields.supplier) != expected:
        return CheckResult(
            name, False, f"{fields.part_number} is not supplied by {fields.supplier!r}"
        )
    return CheckResult(name, True)


def check_date(fields: ExtractedFields, today: date) -> CheckResult:
    if fields.detected_on is None:
        return CheckResult("date_plausible", False, "no detection date in the report")
    if fields.detected_on > today:
        return CheckResult("date_plausible", False, f"{fields.detected_on} is in the future")
    if (today - fields.detected_on).days > 365:
        return CheckResult("date_plausible", False, f"{fields.detected_on} is over a year old")
    return CheckResult("date_plausible", True)


def check_single_defect(report_text: str) -> CheckResult:
    """One report becomes one ticket. A second part number means a second defect was left out."""
    numbers = sorted(set(re.findall(PART_NUMBER_PATTERN, report_text)))
    if len(numbers) > 1:
        return CheckResult(
            "single_defect",
            False,
            f"report mentions {len(numbers)} part numbers ({', '.join(numbers)}); "
            "only the first defect was extracted",
        )
    return CheckResult("single_defect", True)


def check_critical_language(fields: ExtractedFields, report_text: str) -> CheckResult:
    """Safety net behind the model: safety or stop-shipment wording without a critical rating."""
    name = "critical_language"
    if fields.severity == "critical":
        return CheckResult(name, True, "already rated critical")
    match = re.search(CRITICAL_LANGUAGE, report_text, re.IGNORECASE)
    if match:
        return CheckResult(
            name, False, f"report says {match.group(0)!r} but severity is {fields.severity}"
        )
    return CheckResult(name, True)


def validate(
    fields: ExtractedFields,
    report_text: str,
    master: MasterData,
    today: date,
    required: tuple[str, ...] = FIELD_NAMES,
) -> list[CheckResult]:
    return [
        check_required_fields(fields, required),
        _exists(
            "order_exists",
            "order number",
            fields.order_number,
            fields.order_number in master.orders,
        ),
        _exists(
            "part_exists",
            "part number",
            fields.part_number,
            fields.part_number in master.part_suppliers,
        ),
        _exists(
            "supplier_exists",
            "supplier",
            fields.supplier,
            _normalise(fields.supplier or "") in master.suppliers,
        ),
        check_supplier_matches_part(fields, master),
        _exists(
            "station_known", "station", fields.detected_at, fields.detected_at in master.stations
        ),
        check_date(fields, today),
        check_single_defect(report_text),
        check_critical_language(fields, report_text),
    ]
