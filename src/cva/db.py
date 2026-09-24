"""SQLite storage.

Raw upstream JSON is kept alongside the extracted columns so we can
re-derive fields later without re-crawling.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS = [
    """
    CREATE TABLE gazettes (
        id              INTEGER PRIMARY KEY,
        number          TEXT NOT NULL,   -- as listed upstream, e.g. '09', '242B'
        number_norm     TEXT NOT NULL,   -- leading zeros stripped
        year            INTEGER NOT NULL,
        chamber         TEXT NOT NULL,
        date            TEXT NOT NULL,
        doc_type        TEXT NOT NULL DEFAULT '',
        downloadable    INTEGER NOT NULL,
        row_index       INTEGER,         -- position in the latest index crawl
        first_seen_at   TEXT NOT NULL,
        last_seen_at    TEXT NOT NULL,
        -- pending | ok | missing (listed, no file) | unavailable (no
        -- download button) | error
        fetch_status    TEXT NOT NULL DEFAULT 'pending',
        fetch_attempts  INTEGER NOT NULL DEFAULT 0,
        last_error      TEXT,
        fetched_at      TEXT,
        sha256          TEXT,
        size            INTEGER,
        UNIQUE (number, chamber, date)
    );
    CREATE INDEX gazettes_year_number ON gazettes (year, number_norm);
    CREATE INDEX gazettes_fetch ON gazettes (fetch_status);

    CREATE TABLE bills (
        id                INTEGER PRIMARY KEY,
        titulo            TEXT,
        alias             TEXT,
        fecha_radicacion  TEXT,
        numero_camara     TEXT,
        numero_senado     TEXT,
        activo            INTEGER,
        first_seen_at     TEXT NOT NULL,
        last_seen_at      TEXT NOT NULL,
        detail_json       TEXT,
        detail_sha        TEXT,
        detail_fetched_at TEXT,
        latest_state_type INTEGER,
        is_final          INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE bill_states (
        id             INTEGER PRIMARY KEY,
        bill_id        INTEGER NOT NULL REFERENCES bills (id),
        fecha          TEXT,
        state_type_id  INTEGER,
        state_type     TEXT,
        corporacion_id INTEGER,
        gaceta_texto   TEXT,
        gaceta_url     TEXT,
        raw_json       TEXT NOT NULL
    );
    CREATE INDEX bill_states_bill ON bill_states (bill_id);

    CREATE TABLE bill_state_gazettes (
        state_id INTEGER NOT NULL REFERENCES bill_states (id) ON DELETE CASCADE,
        year     INTEGER NOT NULL,
        number   TEXT NOT NULL,
        PRIMARY KEY (state_id, year, number)
    );
    CREATE INDEX bill_state_gazettes_ref ON bill_state_gazettes (year, number);

    CREATE TABLE votes (
        id             INTEGER PRIMARY KEY,
        bill_id        INTEGER,
        fecha          TEXT,
        es_plenaria    INTEGER,
        es_comision    INTEGER,
        acta           TEXT,
        url_gaceta     TEXT,
        has_roll_call  INTEGER NOT NULL,  -- datos_importacion present
        activo         INTEGER,
        raw_json       TEXT NOT NULL,
        raw_sha        TEXT NOT NULL,
        first_seen_at  TEXT NOT NULL,
        last_seen_at   TEXT NOT NULL,
        changed_at     TEXT NOT NULL
    );

    -- One row per (gazette, parser version) so parser iterations can be
    -- compared and re-run without touching fetch state.
    CREATE TABLE parses (
        gazette_id     INTEGER NOT NULL REFERENCES gazettes (id),
        parser_version TEXT NOT NULL,
        status         TEXT NOT NULL,  -- ok | error
        error          TEXT,
        result_json    TEXT,
        processed_at   TEXT NOT NULL,
        PRIMARY KEY (gazette_id, parser_version)
    );

    CREATE TABLE runs (
        id          INTEGER PRIMARY KEY,
        job         TEXT NOT NULL,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        status      TEXT NOT NULL DEFAULT 'running',
        stats_json  TEXT
    );
    """,
    """
    -- One row per person. id is Congreso Visible's persona id, so it is
    -- stable across re-syncs; it is what vote data should reference.
    -- start_date/end_date span all terms and party is the party of the
    -- latest term; per-term detail is in legislator_terms.
    CREATE TABLE legislators (
        id             INTEGER PRIMARY KEY,
        name           TEXT NOT NULL,            -- given names then surnames
        start_date     TEXT,
        end_date       TEXT,
        party          TEXT,
        photo_url      TEXT
    );

    -- One row per (legislator, chamber, four-year term). Dates are the
    -- term's constitutional dates (20 July to 19 July), not the dates a
    -- replacement actually sat.
    CREATE TABLE legislator_terms (
        legislator_id  INTEGER NOT NULL REFERENCES legislators (id),
        chamber        TEXT NOT NULL,  -- same values as gazettes.chamber
        start_date     TEXT NOT NULL,
        end_date       TEXT NOT NULL,
        party          TEXT,
        raw_json       TEXT NOT NULL,
        last_seen_at   TEXT NOT NULL,
        PRIMARY KEY (legislator_id, chamber, start_date)
    );
    CREATE INDEX legislator_terms_dates ON legislator_terms (chamber, start_date, end_date);
    """,
]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {i}")
