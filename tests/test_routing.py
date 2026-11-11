import pytest

from defect_automation.routing import Route, decide_route
from defect_automation.schemas import FIELD_NAMES, ExtractedFields
from defect_automation.validation import CheckResult
from tests.conftest import VALID_ANSWER

HIGH = {name: 0.97 for name in FIELD_NAMES}
PASSED = [CheckResult("order_exists", True), CheckResult("part_exists", True)]


def decide(
    confidence: dict[str, float] | None = None,
    checks: list[CheckResult] | None = None,
    **changes: object,
):
    answer = {k: v for k, v in VALID_ANSWER.items() if k != "confidence"}
    fields = ExtractedFields.model_validate({**answer, **changes})
    return decide_route(fields, confidence or HIGH, checks or PASSED, 0.85, FIELD_NAMES)


@pytest.mark.parametrize("severity", ["minor", "major"])
def test_clean_minor_or_major_report_is_auto_approved(severity: str) -> None:
    decision = decide(severity=severity)
    assert decision.route is Route.AUTO_APPROVE
    assert decision.reasons == []


def test_critical_always_goes_to_review_even_when_everything_else_is_perfect() -> None:
    decision = decide(confidence={name: 1.0 for name in FIELD_NAMES}, severity="critical")
    assert decision.route is Route.REVIEW
    assert decision.reasons == ["severity is critical"]


def test_unknown_severity_goes_to_review() -> None:
    assert decide(severity=None).route is Route.REVIEW


def test_missing_required_field_goes_to_review() -> None:
    decision = decide(order_number=None)
    assert decision.route is Route.REVIEW
    assert "order_number is missing" in decision.reasons


def test_confidence_below_threshold_goes_to_review() -> None:
    decision = decide(confidence={**HIGH, "severity": 0.84})
    assert decision.route is Route.REVIEW
    assert decision.reasons == ["severity confidence 0.84 is below 0.85"]


def test_confidence_exactly_at_threshold_passes() -> None:
    assert decide(confidence={**HIGH, "severity": 0.85}).route is Route.AUTO_APPROVE


def test_missing_confidence_value_goes_to_review() -> None:
    confidence = {k: v for k, v in HIGH.items() if k != "supplier"}
    assert decide(confidence=confidence).route is Route.REVIEW


def test_failed_check_goes_to_review_with_the_mismatch_shown() -> None:
    failed = CheckResult("part_exists", False, "part number 'XX-1' not found in master data")
    decision = decide(checks=[*PASSED, failed])
    assert decision.route is Route.REVIEW
    assert decision.reasons == ["part_exists: part number 'XX-1' not found in master data"]
