"""Build fictional master data as CSV files. Deterministic: same seed, same files.

Run: python -m scripts.generate_master_data

Everything here is invented. No real orders, suppliers or people.
"""

import csv
import itertools
import random
from datetime import date
from pathlib import Path

from faker import Faker

from defect_automation.config import get_settings

SEED = 42

# (name, country, part category supplied)
SUPPLIERS: list[tuple[str, str, str]] = [
    ("Nordhaven Schalttechnik GmbH", "Germany", "circuit_breaker"),
    ("Kestrel Interrupter Works Ltd", "United Kingdom", "circuit_breaker"),
    ("Tessaro Interruttori S.r.l.", "Italy", "circuit_breaker"),
    ("Moravia Kontakt s.r.o.", "Czech Republic", "disconnector"),
    ("Altenbruck Trennschalter GmbH", "Germany", "disconnector"),
    ("Lindqvara Instrument AB", "Sweden", "current_transformer"),
    ("Zelenka Transformatory s.r.o.", "Czech Republic", "current_transformer"),
    ("Ostwerk Aluminiumguss GmbH", "Germany", "enclosure"),
    ("Castellan Fundiciones S.A.", "Spain", "enclosure"),
    ("Varnagy Castings Kft.", "Hungary", "enclosure"),
    ("Rheintal Dichtungstechnik AG", "Switzerland", "gasket"),
    ("Halden Seals Sp. z o.o.", "Poland", "gasket"),
    ("Brennholt Elastomer GmbH", "Austria", "gasket"),
    ("Wexmoor Fasteners Ltd", "United Kingdom", "bolt"),
    ("Solvik Verbindungstechnik GmbH", "Germany", "bolt"),
]
CONTACT_ROLES = ["Quality Manager", "Customer Quality Engineer", "Key Account Manager"]

STATIONS: list[tuple[str, str]] = [
    ("incoming_inspection", "Incoming inspection"),
    ("pre_assembly", "Pre-assembly"),
    ("main_assembly", "Main assembly"),
    ("wiring_check", "Wiring check"),
    ("gas_filling", "Gas filling"),
    ("gas_leak_test", "Gas leak test"),
    ("mechanical_operation_test", "Mechanical operation test"),
    ("high_voltage_test", "High voltage test"),
    ("partial_discharge_test", "Partial discharge test"),
    ("final_inspection", "Final inspection"),
    ("packing", "Packing"),
    ("dispatch", "Dispatch"),
]

VOLTAGES_KV = [72, 145, 245, 420]
ENCLOSURE_VARIANTS = ["busbar section", "breaker housing", "end cover", "T-piece", "cable box"]
PROJECT_NAMES = [
    "Nordwind", "Elbtal", "Hohenfeld", "Seebruck", "Westhafen", "Falkenried", "Steinau",
    "Lindholm", "Marienwerk", "Kaltenbach", "Osterloh", "Grunwald Sud", "Brackwede Ost",
    "Tannenhof", "Auenland", "Riedberg", "Sandkamp", "Wolkenstein", "Hafenkante", "Birkenau",
]  # fmt: skip
PROJECT_KINDS = ["Substation", "Grid Connection", "Offshore Platform", "Converter Station"]
BAY_TYPES = ["line bay", "transformer bay", "bus coupler bay", "cable bay", "measurement bay"]


def build_suppliers(rng: random.Random) -> list[dict[str, str]]:
    return [
        {
            "supplier_id": f"SUP-{index:03d}",
            "name": name,
            "country": country,
            "contact_role": rng.choice(CONTACT_ROLES),
            "category": category,
        }
        for index, (name, country, category) in enumerate(SUPPLIERS, start=1)
    ]


def _part_specs(rng: random.Random) -> list[tuple[str, str, str]]:
    """Return (part_number, description, category) for 80 parts."""
    specs: list[tuple[str, str, str]] = []

    breaker = list(itertools.product(VOLTAGES_KV, [1250, 2000, 2500, 3150, 4000]))
    for kv, amp in rng.sample(breaker, 12):
        specs.append(
            (f"CB-{kv}-{amp}", f"Circuit breaker pole, {kv} kV, {amp} A", "circuit_breaker")
        )

    disconnector = list(itertools.product(VOLTAGES_KV, [1250, 2000, 2500, 3150]))
    for kv, amp in rng.sample(disconnector, 12):
        specs.append((f"DS-{kv}-{amp}", f"Disconnector, {kv} kV, {amp} A", "disconnector"))

    transformer = list(itertools.product(VOLTAGES_KV, [600, 800, 1200, 1600, 2000, 2400]))
    for kv, ratio in rng.sample(transformer, 12):
        specs.append(
            (
                f"CT-{kv}-{ratio:04d}",
                f"Current transformer, {kv} kV, {ratio}/1 A",
                "current_transformer",
            )
        )

    for serial in rng.sample(range(100, 9999), 14):
        kv = rng.choice(VOLTAGES_KV)
        variant = rng.choice(ENCLOSURE_VARIANTS)
        specs.append(
            (f"EN-{kv}-{serial:04d}", f"Enclosure casting, {variant}, {kv} kV", "enclosure")
        )

    for serial in rng.sample(range(100, 9999), 15):
        kv = rng.choice(VOLTAGES_KV)
        diameter = rng.choice([180, 240, 320, 410, 560])
        kind = rng.choice(["Flange gasket", "O-ring seal"])
        specs.append((f"GK-{kv}-{serial:04d}", f"{kind}, {diameter} mm, {kv} kV", "gasket"))

    bolts = list(itertools.product([10, 12, 16, 20], [40, 60, 80, 100, 120]))
    for size, length in rng.sample(bolts, 15):
        specs.append((f"BL-M{size}-{length:04d}", f"Hex bolt M{size}x{length}, A4-80", "bolt"))

    return specs


def build_parts(rng: random.Random, suppliers: list[dict[str, str]]) -> list[dict[str, str]]:
    parts = []
    for part_number, description, category in _part_specs(rng):
        candidates = [s for s in suppliers if s["category"] == category]
        parts.append(
            {
                "part_number": part_number,
                "description": description,
                "category": category,
                "supplier_id": rng.choice(candidates)["supplier_id"],
            }
        )
    return sorted(parts, key=lambda p: p["part_number"])


def build_orders(rng: random.Random, fake: Faker) -> list[dict[str, str]]:
    orders = []
    for number in sorted(rng.sample(range(1, 1000), 50)):
        kv = rng.choice(VOLTAGES_KV)
        project = f"{rng.choice(PROJECT_NAMES)} {kv} kV {rng.choice(PROJECT_KINDS)}"
        due = fake.date_between(date(2026, 12, 1), date(2027, 9, 30))
        orders.append(
            {
                "order_number": f"SG-2026-{number:04d}",
                "customer_project": project,
                "bay_type": rng.choice(BAY_TYPES),
                "due_date": due.isoformat(),
            }
        )
    return orders


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    rng = random.Random(SEED)
    fake = Faker("en_GB")
    fake.seed_instance(SEED)
    out = get_settings().master_dir

    suppliers = build_suppliers(rng)
    parts = build_parts(rng, suppliers)
    orders = build_orders(rng, fake)
    stations = [{"station_code": code, "name": name} for code, name in STATIONS]

    write_csv(out / "suppliers.csv", suppliers, ["supplier_id", "name", "country", "contact_role"])
    write_csv(out / "parts.csv", parts, ["part_number", "description", "category", "supplier_id"])
    write_csv(
        out / "orders.csv", orders, ["order_number", "customer_project", "bay_type", "due_date"]
    )
    write_csv(out / "stations.csv", stations, ["station_code", "name"])

    print(f"Wrote master data to {out}/")
    print(
        f"  suppliers {len(suppliers)}, parts {len(parts)}, orders {len(orders)}, "
        f"stations {len(stations)}"
    )


if __name__ == "__main__":
    main()
