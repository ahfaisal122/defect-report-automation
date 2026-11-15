"""Routing rules (FR-06). One pure function decides: human review or auto-approve.

The rules live here, in code, and never in the prompt. Auto-approval needs every rule
to pass. Anything else, including anything unexpected, goes to a person.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from defect_automation.domain import Severity
from defect_automation.schemas import ExtractedFields
from defect_automation.validation import CheckResult

# A check that fails only because its field is missing adds nothing to "<field> is missing".
CHECK_FIELD = {
    "order_exists": "order_number",
    "part_exists": "part_number",
    "supplier_exists": "supplier",
    "station_known": "detected_at",
    "date_plausible": "detected_on",
}


class Route(StrEnum):
    REVIEW = "review"
    AUTO_APPROVE = "auto_approve"


@dataclass(frozen=True)
class RouteDecision:
    route: Route
    reasons: list[str] = field(default_factory=list)


def decide_route(
    fields: ExtractedFields,
    confidence: dict[str, float],
    checks: list[CheckResult],
    threshold: float,
    required: tuple[str, ...],
) -> RouteDecision:
    reasons: list[str] = []

    if fields.severity == Severity.CRITICAL:
        reasons.append("severity is critical")
    elif fields.severity not in (Severity.MINOR, Severity.MAJOR):
        reasons.append("severity is unknown")

    values = fields.model_dump()
    missing = {name for name in required if values[name] in (None, "")}
    for name in required:
        if name in missing:
            reasons.append(f"{name} is missing")
            continue
        score = confidence.get(name)
        if score is None or score < threshold:
            shown = "none" if score is None else f"{score:.2f}"
            reasons.append(f"{name} confidence {shown} is below {threshold:.2f}")

    for check in checks:
        if check.passed or check.name == "required_fields":  # missing fields are listed above
            continue
        if CHECK_FIELD.get(check.name) not in missing:
            reasons.append(f"{check.name}: {check.message}")

    return RouteDecision(Route.REVIEW if reasons else Route.AUTO_APPROVE, reasons)
