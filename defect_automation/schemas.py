"""The fixed extraction schema (FR-03, FR-04). Malformed model answers fail here."""

from datetime import date

from pydantic import BaseModel, Field

from defect_automation.domain import DefectType, Severity

FIELD_NAMES: tuple[str, ...] = (
    "order_number",
    "part_number",
    "supplier",
    "defect_type",
    "defect_description",
    "quantity_affected",
    "severity",
    "detected_at",
    "detected_on",
    "reporter",
)


class ExtractedFields(BaseModel):
    """The 10 ticket fields. A value the report does not contain is null, never a guess."""

    order_number: str | None
    part_number: str | None
    supplier: str | None
    defect_type: DefectType | None
    defect_description: str | None
    quantity_affected: int | None = Field(ge=1)
    severity: Severity | None
    detected_at: str | None
    detected_on: date | None
    reporter: str | None


class FieldConfidence(BaseModel):
    order_number: float = Field(ge=0, le=1)
    part_number: float = Field(ge=0, le=1)
    supplier: float = Field(ge=0, le=1)
    defect_type: float = Field(ge=0, le=1)
    defect_description: float = Field(ge=0, le=1)
    quantity_affected: float = Field(ge=0, le=1)
    severity: float = Field(ge=0, le=1)
    detected_at: float = Field(ge=0, le=1)
    detected_on: float = Field(ge=0, le=1)
    reporter: float = Field(ge=0, le=1)


class ExtractionOutput(ExtractedFields):
    """What the model must return: the fields plus one confidence value per field."""

    confidence: FieldConfidence

    def fields(self) -> ExtractedFields:
        return ExtractedFields(**self.model_dump(exclude={"confidence"}))

    def report_confidence(self) -> float:
        """A report is only as certain as its least certain field. Null fields count as 0."""
        values = self.model_dump(exclude={"confidence"})
        scores = self.confidence.model_dump()
        return min(0.0 if values[name] is None else scores[name] for name in FIELD_NAMES)
