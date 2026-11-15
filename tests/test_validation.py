from datetime import date, timedelta

import pytest

from defect_automation.schemas import ExtractedFields
from defect_automation.validation import CheckResult, MasterData, validate

TODAY = date(2026, 11, 11)
MASTER = MasterData(
    orders=frozenset({"SG-2026-0003"}),
    part_suppliers={"BL-M10-0060": "wexmoorfastenersltd", "GK-245-3556": "haldensealsspzoo"},
    suppliers=frozenset({"wexmoorfastenersltd", "haldensealsspzoo"}),
    stations=frozenset({"incoming_inspection", "gas_leak_test"}),
)
GOOD = {
    "order_number": "SG-2026-0003",
    "part_number": "BL-M10-0060",
    "supplier": "Wexmoor Fasteners Ltd",
    "defect_type": "dimensional",
    "defect_description": "Bolts too short.",
    "quantity_affected": 20,
    "severity": "minor",
    "detected_at": "incoming_inspection",
    "detected_on": "2026-11-03",
    "reporter": "[PERSON_1]",
}
TEXT = "20 bolts BL-M10-0060 are too short, found at incoming inspection."


def run(text: str = TEXT, **changes: object) -> dict[str, CheckResult]:
    fields = ExtractedFields.model_validate({**GOOD, **changes})
    return {check.name: check for check in validate(fields, text, MASTER, TODAY)}


def test_clean_report_passes_every_check() -> None:
    assert all(check.passed for check in run().values())


@pytest.mark.parametrize(
    ("check", "changes", "expected"),
    [
        ("order_exists", {"order_number": "SG-2026-9999"}, "not found in master data"),
        ("order_exists", {"order_number": None}, "no order number"),
        ("part_exists", {"part_number": "BL-M10-0061"}, "not found in master data"),
        ("supplier_exists", {"supplier": "Unknown Works AG"}, "not found in master data"),
        ("station_known", {"detected_at": "canteen"}, "not found in master data"),
        ("required_fields", {"quantity_affected": None, "reporter": None}, "quantity_affected"),
    ],
)
def test_master_data_mismatch_is_flagged(
    check: str, changes: dict[str, object], expected: str
) -> None:
    result = run(**changes)[check]
    assert not result.passed
    assert expected in result.message


def test_supplier_name_matches_despite_case_and_punctuation() -> None:
    assert run(supplier="wexmoor fasteners ltd.")["supplier_exists"].passed


def test_known_supplier_that_does_not_supply_the_part_is_flagged() -> None:
    result = run(supplier="Halden Seals Sp. z o.o.")
    assert result["supplier_exists"].passed
    assert not result["supplier_matches_part"].passed


def test_future_and_very_old_dates_are_flagged() -> None:
    assert not run(detected_on=TODAY + timedelta(days=1))["date_plausible"].passed
    assert not run(detected_on=TODAY - timedelta(days=400))["date_plausible"].passed
    assert run(detected_on=TODAY)["date_plausible"].passed


def test_second_part_number_in_the_text_is_flagged() -> None:
    text = TEXT + " Also 2 gaskets GK-245-3556 are scratched. Order SG-2026-0003."
    result = run(text)["single_defect"]
    assert not result.passed
    assert "GK-245-3556" in result.message


def test_repeated_part_number_and_order_number_do_not_count_as_a_second_defect() -> None:
    assert run(TEXT + " Order SG-2026-0003. Again: BL-M10-0060.")["single_defect"].passed


@pytest.mark.parametrize(
    "sentence",
    [
        "This is a safety risk for the operators.",
        "We had to stop the shipment of the whole bay.",
        "Production is on hold until this is solved.",
        "The situation is critical.",
    ],
)
def test_critical_wording_without_a_critical_rating_is_flagged(sentence: str) -> None:
    assert not run(f"{TEXT} {sentence}", severity="major")["critical_language"].passed


def test_critical_wording_is_fine_when_already_rated_critical() -> None:
    assert run(TEXT + " This is a safety risk.", severity="critical")["critical_language"].passed
