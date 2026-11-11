"""Load the master data CSVs into SQLite. Safe to run again: rows are replaced by key."""

import csv
import sqlite3
from pathlib import Path

# table -> (csv file, columns in insert order). Suppliers first: parts reference them.
MASTER_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "master_suppliers": ("suppliers.csv", ("supplier_id", "name", "country", "contact_role")),
    "master_parts": ("parts.csv", ("part_number", "description", "category", "supplier_id")),
    "master_orders": ("orders.csv", ("order_number", "customer_project", "bay_type", "due_date")),
    "master_stations": ("stations.csv", ("station_code", "name")),
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def seed_master_data(conn: sqlite3.Connection, master_dir: Path) -> dict[str, int]:
    """Returns the row count per table after loading."""
    counts: dict[str, int] = {}
    for table, (file_name, columns) in MASTER_TABLES.items():
        rows = read_csv(master_dir / file_name)
        placeholders = ", ".join("?" for _ in columns)
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT DO UPDATE SET "
            + ", ".join(f"{col} = excluded.{col}" for col in columns[1:]),
            [tuple(row[col] for col in columns) for row in rows],
        )
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.commit()
    return counts
