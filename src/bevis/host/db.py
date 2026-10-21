"""The SQLite file that holds all host state (SPEC §11)."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE reports (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    created_by       TEXT NOT NULL,          -- author JSON
    creator_key      TEXT,                   -- agent key that created it
    revision         INTEGER NOT NULL,       -- bumps on every draft change
    draft            TEXT NOT NULL,          -- {blocks, claims, non_data}
    draft_validation TEXT NOT NULL           -- {counts, findings}
);

CREATE TABLE evidence (
    report_id        TEXT NOT NULL REFERENCES reports(id),
    id               TEXT NOT NULL,
    version          INTEGER NOT NULL,
    doc              TEXT NOT NULL,          -- the evidence-file encoding, full disclosure
    data_hash        TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    created_by       TEXT NOT NULL,
    PRIMARY KEY (report_id, id, version)
);

CREATE TABLE evidence_runs (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id        TEXT NOT NULL REFERENCES reports(id),
    evidence_id      TEXT NOT NULL,
    version          INTEGER NOT NULL,       -- the version this run created or matched
    outcome          TEXT NOT NULL,          -- created | new_version | same_result
    flags            TEXT NOT NULL,          -- ["query text changed", ...]
    source           TEXT NOT NULL,
    sql              TEXT NOT NULL,
    params           TEXT NOT NULL,
    run_at           TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    author           TEXT NOT NULL
);

CREATE TABLE versions (
    report_id        TEXT NOT NULL REFERENCES reports(id),
    number           INTEGER NOT NULL,
    title            TEXT NOT NULL,
    published_at     TEXT NOT NULL,
    published_by     TEXT NOT NULL,
    override         TEXT,                   -- {reason, errors_overridden} or NULL
    content          TEXT NOT NULL,          -- {blocks, claims, non_data}, statuses filled in
    validation       TEXT NOT NULL,          -- {counts, findings} at publish
    PRIMARY KEY (report_id, number)
);

CREATE TABLE suggestions (
    id               TEXT PRIMARY KEY,
    report_id        TEXT NOT NULL REFERENCES reports(id),
    author           TEXT NOT NULL,
    message          TEXT NOT NULL,
    ops              TEXT NOT NULL,
    base_revision    INTEGER NOT NULL,
    status           TEXT NOT NULL,          -- open | accepted | rejected
    created_at       TEXT NOT NULL,
    resolved_at      TEXT,
    resolved_by      TEXT,
    note             TEXT
);

CREATE TABLE comments (
    id               TEXT PRIMARY KEY,
    report_id        TEXT NOT NULL REFERENCES reports(id),
    target           TEXT NOT NULL,          -- {"block": id} | {"claim": id} | {}
    version          INTEGER,                -- published version, or NULL for the draft
    body             TEXT NOT NULL,
    author           TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE activity (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id        TEXT NOT NULL REFERENCES reports(id),
    author           TEXT NOT NULL,
    action           TEXT NOT NULL,
    before           TEXT,
    after            TEXT,
    timestamp        TEXT NOT NULL
);

CREATE TABLE credentials (
    id               TEXT PRIMARY KEY,
    secret_hash      TEXT NOT NULL UNIQUE,   -- SHA-256 of the secret; the secret is never stored
    kind             TEXT NOT NULL,          -- agent_key | link
    name             TEXT NOT NULL,
    author_kind      TEXT NOT NULL,          -- agent | human
    report_id        TEXT REFERENCES reports(id),
    role             TEXT,                   -- links: reader .. admin
    disclosure       TEXT,                   -- links: full | cited | metadata
    scope            TEXT,                   -- links: draft | latest | <version number>
    expires_at       TEXT,
    revoked_at       TEXT,
    created_at       TEXT NOT NULL,
    created_by       TEXT
);

CREATE INDEX activity_report ON activity (report_id, seq);
CREATE INDEX runs_report ON evidence_runs (report_id, evidence_id, seq);
CREATE INDEX suggestions_report ON suggestions (report_id, status);
CREATE INDEX comments_report ON comments (report_id, created_at);
CREATE INDEX credentials_report ON credentials (report_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


class Database:
    """One SQLite file. Reads run concurrently; writes are serialized."""

    def __init__(self, path: Path):
        self.path = path
        self._write_lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                conn.executescript(SCHEMA)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise RuntimeError(f"{path}: schema version {version}, expected {SCHEMA_VERSION}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One transaction. It commits on success and rolls back on any exception."""
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
