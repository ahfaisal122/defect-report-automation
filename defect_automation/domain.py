"""Fixed vocabularies shared by the data generator, the extraction prompt and the rules."""

from enum import StrEnum


class DefectType(StrEnum):
    GAS_LEAK = "gas_leak"
    ELECTRICAL_FAILURE = "electrical_failure"
    DIMENSIONAL = "dimensional"
    SURFACE_DAMAGE = "surface_damage"
    MISSING_PART = "missing_part"
    WRONG_PART = "wrong_part"
    CONTAMINATION = "contamination"
    ASSEMBLY_ERROR = "assembly_error"
    DOCUMENTATION = "documentation"
    OTHER = "other"


class Severity(StrEnum):
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class ReportStatus(StrEnum):
    RECEIVED = "received"
    PARSED = "parsed"
    EXTRACTED = "extracted"
    VALIDATED = "validated"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    POSTED = "posted"
    FAILED = "failed"


SEVERITY_RUBRIC: dict[Severity, str] = {
    Severity.MINOR: (
        "cosmetic or easily reworked at the station; no effect on function or delivery date"
    ),
    Severity.MAJOR: (
        "part unusable or function affected; needs replacement or supplier action; "
        "may delay the order"
    ),
    Severity.CRITICAL: (
        "safety risk, risk of gas release or electrical failure in service, "
        "or a stop of production or shipment"
    ),
}

DEFECT_TYPE_DESCRIPTIONS: dict[DefectType, str] = {
    DefectType.GAS_LEAK: "insulating gas escapes; failed tightness or pressure-holding test",
    DefectType.ELECTRICAL_FAILURE: (
        "flashover, partial discharge above limit, low insulation resistance"
    ),
    DefectType.DIMENSIONAL: "a measurement is outside the drawing tolerance; part does not fit",
    DefectType.SURFACE_DAMAGE: "scratches, dents, chipped coating, corrosion",
    DefectType.MISSING_PART: "items are missing or the delivered quantity is short",
    DefectType.WRONG_PART: "the delivered item is a different part or variant than ordered",
    DefectType.CONTAMINATION: "swarf, dust, oil or other foreign material in or on the part",
    DefectType.ASSEMBLY_ERROR: "wrong torque, wrong orientation, wrong wiring, wrongly mounted",
    DefectType.DOCUMENTATION: "certificate or test report missing or not matching the delivery",
    DefectType.OTHER: "none of the above",
}

ORDER_NUMBER_PATTERN = r"SG-\d{4}-\d{4}"
PART_NUMBER_PATTERN = r"\b[A-Z]{2}-M?\d{2,3}-\d{4}\b"

# Wording that signals a critical defect, taken from the severity rubric. A report that
# uses it without a critical rating goes to a person, whatever the model concluded.
CRITICAL_LANGUAGE = (
    r"\bsafety\b|\bhazard|\bdanger|\binjur|\bcritical\b|\bexplosi"
    r"|\b(?:stop|stopp|halt|hold|block)\w*\b[^.\n]{0,40}\b(?:shipment|shipping|deliver|production)"
    r"|\b(?:shipment|shipping|production)\w*\b[^.\n]{0,25}\b(?:stop|halt|on hold|blocked)"
)
