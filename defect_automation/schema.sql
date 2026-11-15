-- One SQLite file holds the whole flow. The reports table doubles as the work queue.

CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('txt', 'eml')),
    file_name   TEXT NOT NULL,
    file_hash   TEXT NOT NULL UNIQUE,          -- blocks duplicate ingestion
    raw_text    TEXT NOT NULL,                 -- original content, unmasked
    status      TEXT NOT NULL DEFAULT 'received' CHECK (status IN (
                    'received', 'parsed', 'extracted', 'validated', 'in_review',
                    'approved', 'rejected', 'posted', 'failed')),
    received_at TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports (status);

CREATE TABLE IF NOT EXISTS extractions (
    id              INTEGER PRIMARY KEY,
    report_id       INTEGER NOT NULL REFERENCES reports (id),
    fields_json     TEXT NOT NULL,
    confidence_json TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    cost_eur        REAL NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extractions_report ON extractions (report_id);

CREATE TABLE IF NOT EXISTS validations (
    id            INTEGER PRIMARY KEY,
    extraction_id INTEGER NOT NULL REFERENCES extractions (id),
    check_name    TEXT NOT NULL,
    passed        INTEGER NOT NULL CHECK (passed IN (0, 1)),
    message       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_validations_extraction ON validations (extraction_id);

CREATE TABLE IF NOT EXISTS reviews (
    id                 INTEGER PRIMARY KEY,
    report_id          INTEGER NOT NULL REFERENCES reports (id),
    reviewer           TEXT NOT NULL,
    action             TEXT NOT NULL CHECK (action IN ('approve', 'reject')),
    edited_fields_json TEXT,
    reviewed_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY,
    report_id    INTEGER NOT NULL UNIQUE REFERENCES reports (id),  -- one ticket per report
    qm_ticket_id TEXT NOT NULL,
    posted_via   TEXT NOT NULL,
    posted_at    TEXT NOT NULL
);

-- Append-only audit trail (FR-10). Nothing in the code updates or deletes these rows.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL,
    report_id   INTEGER REFERENCES reports (id),
    event       TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_report ON audit_log (report_id);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log (run_id);

-- Master data used by validation.
CREATE TABLE IF NOT EXISTS master_suppliers (
    supplier_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    country      TEXT NOT NULL,
    contact_role TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS master_parts (
    part_number TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    category    TEXT NOT NULL,
    supplier_id TEXT NOT NULL REFERENCES master_suppliers (supplier_id)
);

CREATE TABLE IF NOT EXISTS master_orders (
    order_number     TEXT PRIMARY KEY,
    customer_project TEXT NOT NULL,
    bay_type         TEXT NOT NULL,
    due_date         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS master_stations (
    station_code TEXT PRIMARY KEY,
    name         TEXT NOT NULL
);
