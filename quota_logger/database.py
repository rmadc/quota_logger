"""SQLite schema and connection helpers for the provider-neutral logger."""
from __future__ import annotations

import pathlib
import sqlite3


SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE providers (
    provider_id INTEGER PRIMARY KEY,
    provider_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL
);

CREATE TABLE samples (
    sample_id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES providers(provider_id),
    observed_at_utc TEXT NOT NULL,
    ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
    error TEXT,
    raw_json TEXT,
    origin_key TEXT,
    origin_sample_id INTEGER,
    CHECK ((origin_key IS NULL) = (origin_sample_id IS NULL)),
    UNIQUE (origin_key, origin_sample_id)
);

CREATE TABLE quota_windows (
    sample_id INTEGER NOT NULL REFERENCES samples(sample_id) ON DELETE CASCADE,
    window_key TEXT NOT NULL,
    window_label TEXT NOT NULL,
    used_pct REAL,
    reset_at_epoch INTEGER,
    window_seconds INTEGER CHECK (window_seconds IS NULL OR window_seconds > 0),
    PRIMARY KEY (sample_id, window_key)
);

CREATE INDEX samples_provider_time
    ON samples(provider_id, observed_at_utc, sample_id);
CREATE INDEX quota_windows_key
    ON quota_windows(window_key, sample_id);
"""

PROVIDERS = {
    "codex": "Codex",
    "claude": "Claude",
}


def initialize_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Create the current schema on a new database or reject unknown versions."""
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {version} is newer than supported version {SCHEMA_VERSION}."
        )
    if version == 0:
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if existing:
            raise RuntimeError("Unversioned database already contains tables; refusing to modify it.")
        seed_providers = ", ".join(
            f"('{key}', '{name}')" for key, name in PROVIDERS.items()
        )
        conn.executescript(f"""
            BEGIN IMMEDIATE;
            {SCHEMA_SQL}
            INSERT INTO providers(provider_key, display_name) VALUES {seed_providers};
            PRAGMA user_version = {SCHEMA_VERSION};
            COMMIT;
        """)
    elif version != SCHEMA_VERSION:
        raise RuntimeError(f"No migration path exists from schema version {version}.")
    return conn


def open_database(path: pathlib.Path | str) -> sqlite3.Connection:
    """Open and initialize the unified database at ``path``."""
    target = pathlib.Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        initialize_connection(conn)
    except Exception:
        conn.close()
        raise
    return conn
