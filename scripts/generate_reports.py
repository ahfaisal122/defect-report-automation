"""Generate 30 synthetic defect reports plus labelled answers for 20 of them.

Run: python -m scripts.generate_reports [--force] [--pause 5]

Each report starts from one master data record, so the correct answers are known before
the model writes a word. Gemini only turns the facts into a realistic email. Every email
is then checked in code: the key facts must appear in the text, or it is regenerated.

Scenarios are seeded and deterministic. The wording is not (it comes from the model),
which is why existing files are kept unless --force is given.
"""

import argparse
import json
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from faker import Faker
from pydantic import BaseModel

from defect_automation.config import get_settings
from defect_automation.domain import ORDER_NUMBER_PATTERN, SEVERITY_RUBRIC, DefectType, Severity
from defect_automation.llm import LLMClient, get_llm_client
from defect_automation.seed import read_csv

SEED = 2026
REPORT_COUNT = 30
EML_COUNT = 10
LABELLED_COUNT = 20
CRITICAL_COUNT = 4
MAX_ATTEMPTS = 3
FACTORY_DOMAIN = "voltaris-switchgear.example"
QUALITY_INBOX = f"quality-reports@{FACTORY_DOMAIN}"

FLAVORS = (
    ["clean"] * 12
    + ["signature"] * 4
    + ["forwarded"] * 4
    + ["missing_order"] * 4
    + ["wrong_part"] * 3
    + ["two_defects"] * 3
)

FLAVOR_INSTRUCTIONS = {
    "clean": "A short, direct email. One or two short paragraphs.",
    "signature": (
        "In closing_lines, continue after the sender's name with a long corporate signature: "
        "job title, department, a fictional phone number starting +49 30 5550, and a "
        "multi-sentence legal confidentiality disclaimer."
    ),
    "forwarded": (
        "This email will be forwarded to quality by a colleague, {forwarder}. Also fill "
        "'forward_note': one or two sentences from {forwarder} passing the email on, signed "
        "with their first name. The note must not repeat any defect facts."
    ),
    "missing_order": (
        "The writer forgot the production order. Do NOT mention any order number or any "
        "identifier that looks like one. Refer to the job only vaguely, e.g. 'the bay "
        "currently on the line'."
    ),
    "wrong_part": "Written in a hurry: informal, a few typos in ordinary words.",
    "two_defects": (
        "The email reports TWO separate defects on the same order. Describe the primary "
        "defect first and in more detail. Then add the secondary defect in its own paragraph."
    ),
}

CATEGORY_DEFECTS: dict[str, list[DefectType]] = {
    "circuit_breaker": [
        DefectType.GAS_LEAK,
        DefectType.ELECTRICAL_FAILURE,
        DefectType.ASSEMBLY_ERROR,
        DefectType.SURFACE_DAMAGE,
    ],
    "disconnector": [
        DefectType.DIMENSIONAL,
        DefectType.ASSEMBLY_ERROR,
        DefectType.SURFACE_DAMAGE,
        DefectType.ELECTRICAL_FAILURE,
    ],
    "current_transformer": [
        DefectType.ELECTRICAL_FAILURE,
        DefectType.SURFACE_DAMAGE,
        DefectType.DOCUMENTATION,
        DefectType.WRONG_PART,
    ],
    "enclosure": [
        DefectType.GAS_LEAK,
        DefectType.SURFACE_DAMAGE,
        DefectType.DIMENSIONAL,
        DefectType.CONTAMINATION,
    ],
    "gasket": [
        DefectType.DIMENSIONAL,
        DefectType.SURFACE_DAMAGE,
        DefectType.CONTAMINATION,
        DefectType.WRONG_PART,
    ],
    "bolt": [
        DefectType.DIMENSIONAL,
        DefectType.MISSING_PART,
        DefectType.WRONG_PART,
        DefectType.SURFACE_DAMAGE,
    ],
}
CRITICAL_DEFECTS = {DefectType.GAS_LEAK, DefectType.ELECTRICAL_FAILURE}

DEFECT_STATIONS: dict[DefectType, list[str]] = {
    DefectType.GAS_LEAK: ["gas_leak_test", "gas_filling"],
    DefectType.ELECTRICAL_FAILURE: ["high_voltage_test", "partial_discharge_test"],
    DefectType.DIMENSIONAL: ["incoming_inspection", "pre_assembly", "main_assembly"],
    DefectType.SURFACE_DAMAGE: ["incoming_inspection", "pre_assembly", "final_inspection"],
    DefectType.MISSING_PART: ["pre_assembly", "main_assembly", "packing"],
    DefectType.WRONG_PART: ["incoming_inspection", "main_assembly"],
    DefectType.CONTAMINATION: ["incoming_inspection", "main_assembly", "gas_filling"],
    DefectType.ASSEMBLY_ERROR: ["mechanical_operation_test", "wiring_check", "final_inspection"],
    DefectType.DOCUMENTATION: ["incoming_inspection", "final_inspection"],
}

DEFECT_HINTS: dict[DefectType, list[str]] = {
    DefectType.GAS_LEAK: [
        "Leak found at the flange seal during the gas tightness test",
        "Pressure drop over the holding period, leak located at a weld seam",
        "Leak detector alarm at the bursting disc connection",
    ],
    DefectType.ELECTRICAL_FAILURE: [
        "Flashover during the power-frequency withstand test",
        "Partial discharge level above the permitted limit",
        "Insulation resistance below the specified value",
    ],
    DefectType.DIMENSIONAL: [
        "Bore diameter out of tolerance, part does not fit the mating flange",
        "Length outside drawing tolerance",
        "Hole pattern offset from the drawing",
    ],
    DefectType.SURFACE_DAMAGE: [
        "Scratches on the sealing surface",
        "Dent and chipped coating, probably transport damage",
        "Corrosion spots on the contact surface",
    ],
    DefectType.MISSING_PART: [
        "Items missing from the delivered kit",
        "Delivered quantity short against the delivery note",
    ],
    DefectType.WRONG_PART: [
        "Delivered item does not match the ordered specification",
        "Wrong variant delivered, label and content differ",
    ],
    DefectType.CONTAMINATION: [
        "Metal swarf found inside the part",
        "Oil and dust residue on surfaces that must be clean for gas compartments",
    ],
    DefectType.ASSEMBLY_ERROR: [
        "Fasteners not tightened to the specified torque",
        "Component mounted in the wrong orientation",
        "Auxiliary wiring connected to the wrong terminals",
    ],
    DefectType.DOCUMENTATION: [
        "Material certificate missing from the delivery",
        "Routine test report does not match the serial numbers delivered",
    ],
}

DATE_FORMATS = ["%d %b %Y", "%d.%m.%Y", "%Y-%m-%d", "%d %B %Y"]


@dataclass
class Defect:
    part_number: str
    part_description: str
    supplier: str
    defect_type: str
    hint: str
    quantity_affected: int
    severity: str
    detected_at: str
    station_name: str


@dataclass
class Scenario:
    index: int
    file_name: str
    flavor: str
    order_number: str | None
    primary: Defect
    secondary: Defect | None
    detected_on: date
    date_text: str
    sent_at: datetime
    reporter: str
    forwarder: str | None
    explicit_severity: bool
    labelled: bool

    def label(self) -> dict[str, object]:
        """Ground truth for the eval set: the 10 extraction fields, plus the case type."""
        return {
            "order_number": self.order_number,
            "part_number": self.primary.part_number,
            "supplier": self.primary.supplier,
            "defect_type": self.primary.defect_type,
            "defect_description": self.primary.hint,
            "quantity_affected": self.primary.quantity_affected,
            "severity": self.primary.severity,
            "detected_at": self.primary.detected_at,
            "detected_on": self.detected_on.isoformat(),
            "reporter": self.reporter,
            "case_type": self.flavor,
        }


class GeneratedEmail(BaseModel):
    """The model writes the words. Code assembles the layout, so line breaks are reliable."""

    subject: str
    greeting: str
    paragraphs: list[str]
    closing_lines: list[str]
    forward_note: str = ""


class MasterData:
    def __init__(self, master_dir: Path) -> None:
        self.orders = read_csv(master_dir / "orders.csv")
        self.parts = read_csv(master_dir / "parts.csv")
        self.suppliers = {s["supplier_id"]: s for s in read_csv(master_dir / "suppliers.csv")}
        self.stations = {
            s["station_code"]: s["name"] for s in read_csv(master_dir / "stations.csv")
        }
        self.part_numbers = {p["part_number"] for p in self.parts}


def corrupt_part_number(part_number: str, known: set[str], rng: random.Random) -> str:
    """Change one digit so the number looks plausible but is not in master data."""
    positions = [i for i, char in enumerate(part_number) if char.isdigit()]
    while True:
        chars = list(part_number)
        position = rng.choice(positions)
        chars[position] = rng.choice([d for d in "0123456789" if d != chars[position]])
        candidate = "".join(chars)
        if candidate not in known:
            return candidate


def build_defect(master: MasterData, rng: random.Random, critical: bool) -> Defect:
    parts = master.parts
    if critical:
        parts = [p for p in parts if p["category"] in ("circuit_breaker", "enclosure")]
    part = rng.choice(parts)
    defect_types = CATEGORY_DEFECTS[part["category"]]
    if critical:
        defect_types = [d for d in defect_types if d in CRITICAL_DEFECTS]
    defect_type = rng.choice(defect_types)
    station = rng.choice(DEFECT_STATIONS[defect_type])
    if critical:
        severity, quantity = Severity.CRITICAL, rng.randint(1, 3)
    else:
        severity = rng.choices([Severity.MINOR, Severity.MAJOR], weights=[55, 45])[0]
        if defect_type in CRITICAL_DEFECTS:
            severity = Severity.MAJOR  # a leak or electrical failure is never a minor finding
        bulk = part["category"] in ("gasket", "bolt")
        quantity = rng.choice([5, 10, 12, 20, 24, 50, 100]) if bulk else rng.randint(1, 6)
    return Defect(
        part_number=part["part_number"],
        part_description=part["description"],
        supplier=master.suppliers[part["supplier_id"]]["name"],
        defect_type=defect_type.value,
        hint=rng.choice(DEFECT_HINTS[defect_type]),
        quantity_affected=quantity,
        severity=severity.value,
        detected_at=station,
        station_name=master.stations[station],
    )


def build_scenarios(master: MasterData) -> list[Scenario]:
    rng = random.Random(SEED)
    fake = Faker(["en_GB", "de_DE"])
    fake.seed_instance(SEED)

    flavors = FLAVORS.copy()
    rng.shuffle(flavors)
    critical = set(rng.sample(range(REPORT_COUNT), CRITICAL_COUNT))
    eml = set(rng.sample(range(REPORT_COUNT), EML_COUNT))
    # Every critical case is labelled so critical recall is measured on all of them.
    others = [i for i in range(REPORT_COUNT) if i not in critical]
    labelled = critical | set(rng.sample(others, LABELLED_COUNT - CRITICAL_COUNT))

    scenarios = []
    for index in range(REPORT_COUNT):
        flavor = flavors[index]
        primary = build_defect(master, rng, critical=index in critical)
        if flavor == "wrong_part":
            primary.part_number = corrupt_part_number(primary.part_number, master.part_numbers, rng)
        secondary = None
        if flavor == "two_defects":
            secondary = build_defect(master, rng, critical=False)
            while secondary.part_number == primary.part_number:
                secondary = build_defect(master, rng, critical=False)

        detected_on = date(2026, 9, 1) + timedelta(days=rng.randint(0, 65))
        sent_at = datetime.combine(detected_on, datetime.min.time()) + timedelta(
            days=rng.randint(0, 2), hours=rng.randint(7, 17), minutes=rng.randint(0, 59)
        )
        number = index + 1
        scenarios.append(
            Scenario(
                index=number,
                file_name=f"report_{number:03d}.{'eml' if index in eml else 'txt'}",
                flavor=flavor,
                order_number=(
                    None if flavor == "missing_order" else rng.choice(master.orders)["order_number"]
                ),
                primary=primary,
                secondary=secondary,
                detected_on=detected_on,
                date_text=detected_on.strftime(rng.choice(DATE_FORMATS)),
                sent_at=sent_at,
                reporter=f"{fake.first_name()} {fake.last_name()}",
                forwarder=(
                    f"{fake.first_name()} {fake.last_name()}" if flavor == "forwarded" else None
                ),
                explicit_severity=rng.random() < 0.5,
                labelled=index in labelled,
            )
        )
    return scenarios


def _defect_facts(defect: Defect, explicit_severity: bool) -> str:
    if explicit_severity:
        severity = f'State the severity with the exact word "{defect.severity}".'
    else:
        rubric = SEVERITY_RUBRIC[Severity(defect.severity)]
        severity = (
            f"Do not use the words minor, major or critical. Make the severity "
            f"({defect.severity}) clear from the consequences: {rubric}."
        )
    return (
        f"- Part number, written exactly: {defect.part_number} ({defect.part_description})\n"
        f"- Supplier, written exactly: {defect.supplier}\n"
        f"- What is wrong: {defect.hint}\n"
        f"- Quantity affected, written as a numeral: {defect.quantity_affected}\n"
        f"- Found at this station (use the name naturally): {defect.station_name}\n"
        f"- Severity: {severity}"
    )


def build_prompt(scenario: Scenario) -> str:
    style = FLAVOR_INSTRUCTIONS[scenario.flavor].format(
        forwarder=scenario.forwarder, reporter=scenario.reporter
    )
    order = (
        f"- Production order, written exactly: {scenario.order_number}"
        if scenario.order_number
        else "- Production order: unknown to the writer, not mentioned"
    )
    secondary = ""
    if scenario.secondary:
        secondary = "\n\nSecondary defect:\n" + _defect_facts(scenario.secondary, True)
    return (
        "Write one internal email from an employee of a high-voltage switchgear factory to "
        "the quality department, reporting a defect. It must read like a real shop-floor "
        "email: plain language, no bullet-point form, no field labels, no markdown. Rephrase "
        "the defect in your own words and add small realistic details. Do not invent other "
        "part numbers, order numbers, suppliers or dates.\n\n"
        "Facts:\n"
        f"- The email is sent on {scenario.sent_at.strftime('%A, %d %B %Y')}\n"
        f"{order}\n"
        f"{_defect_facts(scenario.primary, scenario.explicit_severity)}\n"
        f"- Date the defect was found, written exactly like this: {scenario.date_text}. "
        "Only call it 'today' or 'yesterday' if that fits the send date\n"
        f"- Reporter, who signs with this full name: {scenario.reporter}"
        f"{secondary}\n\n"
        f"Style: {style}\n\n"
        "Return JSON with: 'subject' (a short subject line), 'greeting' (one line), "
        "'paragraphs' (the body, one list item per paragraph), 'closing_lines' (the sign-off, "
        "then the sender's full name, then any signature lines, one list item per line) and "
        "'forward_note' (empty unless the style asks for it)."
    )


def forwarded_at(scenario: Scenario) -> datetime:
    """A forward leaves one to four hours after the original."""
    return scenario.sent_at + timedelta(hours=1 + scenario.index % 4, minutes=7 * scenario.index)


def compose_body(scenario: Scenario, email: GeneratedEmail) -> str:
    original = "\n\n".join(
        [email.greeting.strip(), *(p.strip() for p in email.paragraphs)]
        + ["\n".join(line.strip() for line in email.closing_lines)]
    )
    if not scenario.forwarder:
        return original
    return (
        f"{email.forward_note.strip()}\n\n"
        "---------- Forwarded message ----------\n"
        f"From: {scenario.reporter} <{email_address(scenario.reporter)}>\n"
        f"Date: {scenario.sent_at.strftime('%a, %d %b %Y %H:%M')}\n"
        f"Subject: {email.subject}\n"
        f"To: {scenario.forwarder} <{email_address(scenario.forwarder)}>\n\n"
        f"{original}"
    )


def find_problems(scenario: Scenario, email: GeneratedEmail) -> list[str]:
    """Check that the text really carries the facts the label claims."""
    body = compose_body(scenario, email)
    text = f"{email.subject}\n{body}"
    problems = []
    required = {
        "part number": scenario.primary.part_number,
        "supplier": scenario.primary.supplier,
        "reporter": scenario.reporter,
        "date": scenario.date_text,
    }
    if scenario.order_number:
        required["order number"] = scenario.order_number
    if scenario.secondary:
        required["secondary part number"] = scenario.secondary.part_number
    for name, value in required.items():
        if value not in text:
            problems.append(f"{name} {value!r} missing")
    paragraphs = "\n".join(email.paragraphs)
    without_ids = paragraphs.replace(scenario.date_text, "")
    for identifier in (scenario.primary.part_number, scenario.order_number or ""):
        without_ids = without_ids.replace(identifier, "") if identifier else without_ids
    if not re.search(
        rf"(?<![\w.-]){scenario.primary.quantity_affected}(?![\w.-]|,\d)", without_ids
    ):
        problems.append("quantity missing as a numeral")
    orders_in_text = set(re.findall(ORDER_NUMBER_PATTERN, text))
    if orders_in_text - {scenario.order_number}:
        problems.append(f"unexpected order numbers {sorted(orders_in_text)}")
    if scenario.forwarder and len(email.forward_note.strip()) < 10:
        problems.append("forward_note missing")
    if len(paragraphs) < 120:
        problems.append("body too short")
    if not email.closing_lines:
        problems.append("closing_lines empty")
    return problems


def email_address(name: str) -> str:
    ascii_name = (
        name.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    )
    return re.sub(r"[^a-z]+", ".", ascii_name).strip(".") + f"@{FACTORY_DOMAIN}"


def write_report(scenario: Scenario, email: GeneratedEmail, out_dir: Path) -> None:
    sender = scenario.forwarder or scenario.reporter
    sender_header = f"{sender} <{email_address(sender)}>"
    subject = f"FW: {email.subject}" if scenario.forwarder else email.subject
    sent_at = forwarded_at(scenario) if scenario.forwarder else scenario.sent_at
    body = compose_body(scenario, email)
    path = out_dir / scenario.file_name
    if path.suffix == ".eml":
        message = EmailMessage()
        message["From"] = sender_header
        message["To"] = QUALITY_INBOX
        message["Date"] = format_datetime(sent_at)
        message["Subject"] = subject
        message.set_content(body + "\n")
        path.write_bytes(message.as_bytes())
    else:
        header = (
            f"From: {sender_header}\n"
            f"To: {QUALITY_INBOX}\n"
            f"Sent: {sent_at.strftime('%a, %d %b %Y %H:%M')}\n"
            f"Subject: {subject}\n\n"
        )
        path.write_text(header + body + "\n", encoding="utf-8")


def generate_email(client: LLMClient, scenario: Scenario, pause: float) -> GeneratedEmail:
    prompt = build_prompt(scenario)
    problems: list[str] = []
    for _attempt in range(MAX_ATTEMPTS):
        feedback = f"\n\nYour last attempt had these problems: {problems}" if problems else ""
        response = client.generate_json(prompt + feedback, GeneratedEmail, temperature=0.9)
        time.sleep(pause)
        email = GeneratedEmail.model_validate_json(response.text)
        problems = find_problems(scenario, email)
        if not problems:
            return email
        print(f"    retry {scenario.file_name}: {problems}")
    raise RuntimeError(f"{scenario.file_name}: still wrong after {MAX_ATTEMPTS} attempts")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="regenerate existing reports")
    parser.add_argument("--pause", type=float, default=5.0, help="seconds between model calls")
    parser.add_argument("--plan", action="store_true", help="print the scenarios, call no model")
    args = parser.parse_args()

    settings = get_settings()
    scenarios = build_scenarios(MasterData(settings.master_dir))

    if args.plan:
        for s in scenarios:
            print(
                f"{s.file_name}  {s.flavor:<13} {s.primary.severity:<8} "
                f"{s.primary.defect_type:<18} {s.order_number or '-':<12} "
                f"{s.primary.part_number:<12} {'labelled' if s.labelled else ''}"
            )
        return

    settings.input_dir.mkdir(parents=True, exist_ok=True)
    client = get_llm_client(settings)
    print(f"Writing {len(scenarios)} reports with {client.model} into {settings.input_dir}/")
    for scenario in scenarios:
        path = settings.input_dir / scenario.file_name
        if path.exists() and not args.force:
            print(f"  keep  {scenario.file_name}")
            continue
        write_report(scenario, generate_email(client, scenario, args.pause), settings.input_dir)
        print(f"  wrote {scenario.file_name}  ({scenario.flavor}, {scenario.primary.severity})")

    labels = {s.file_name: s.label() for s in scenarios if s.labelled}
    settings.labels_path.parent.mkdir(parents=True, exist_ok=True)
    settings.labels_path.write_text(json.dumps(labels, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(labels)} labelled answers to {settings.labels_path}")


if __name__ == "__main__":
    main()
