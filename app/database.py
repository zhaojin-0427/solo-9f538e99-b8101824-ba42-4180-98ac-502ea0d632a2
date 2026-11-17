"""SQLite schema, connection management and the trace-ledger migration.

The append-only ``trace_events`` table is the authoritative history of every
accepted fragment change and every publication.  Each event stores the full
effective fragment set *after* the change, so any historical sequence can be
reconstructed exactly without replaying (or reinterpreting) later corrections.

Upgrading an existing database seeds one ``baseline`` event per station-minute
from the fragment state present at upgrade time.  Changes that happened before
that baseline were never saved and are therefore reported as unknown, never
guessed.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .assembly import fragment_snapshot, snapshot_hash
from .config import DB_PATH

TRACE_META_KEY = "trace_baseline"
TRACE_META_VALUE = "v1"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS fragments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    station      TEXT    NOT NULL,
    minute_utc   TEXT    NOT NULL,
    fragment_no  INTEGER NOT NULL,
    revision     INTEGER NOT NULL CHECK (revision >= 1),
    start_sample INTEGER NOT NULL CHECK (start_sample >= 0 AND start_sample < 60),
    samples      TEXT    NOT NULL,
    content_hash TEXT    NOT NULL,
    UNIQUE (station, minute_utc, fragment_no)
);

CREATE INDEX IF NOT EXISTS idx_fragments_minute
    ON fragments (station, minute_utc);

CREATE TABLE IF NOT EXISTS publications (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    station          TEXT    NOT NULL,
    minute_utc       TEXT    NOT NULL,
    wave             TEXT    NOT NULL,
    fragment_set_hash TEXT   NOT NULL,
    stale            INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0, 1)),
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_event_seq    INTEGER,
    creation_time_known  INTEGER NOT NULL DEFAULT 1 CHECK (creation_time_known IN (0, 1)),
    invalidated_event_seq INTEGER
);

CREATE INDEX IF NOT EXISTS idx_publications_minute
    ON publications (station, minute_utc);

CREATE TABLE IF NOT EXISTS publication_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    fragment_no    INTEGER NOT NULL,
    revision       INTEGER NOT NULL,
    start_sample   INTEGER NOT NULL,
    samples        TEXT    NOT NULL,
    content_hash   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pub_items_pub
    ON publication_items (publication_id);

-- Append-only history.  seq is a global, monotonically increasing sequence.
CREATE TABLE IF NOT EXISTS trace_events (
    seq                   INTEGER PRIMARY KEY AUTOINCREMENT,
    station               TEXT    NOT NULL,
    minute_utc            TEXT    NOT NULL,
    kind                  TEXT    NOT NULL
        CHECK (kind IN ('baseline', 'fragment_added', 'fragment_revised', 'publish')),
    fragment_no           INTEGER,
    revision              INTEGER,
    previous_revision     INTEGER,
    previous_start_sample INTEGER,
    previous_samples      TEXT,
    previous_content_hash TEXT,
    start_sample          INTEGER,
    samples               TEXT,
    content_hash          TEXT,
    publication_id        INTEGER REFERENCES publications(id) ON DELETE SET NULL,
    snapshot_hash         TEXT    NOT NULL,
    prior_snapshot_hash   TEXT,
    snapshot_json         TEXT    NOT NULL,
    unknown_before        INTEGER NOT NULL DEFAULT 0 CHECK (unknown_before IN (0, 1)),
    recorded_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_trace_events_minute
    ON trace_events (station, minute_utc, seq);

CREATE INDEX IF NOT EXISTS idx_trace_events_publication
    ON trace_events (publication_id);

-- Which fragment change invalidated which publication (one cause per record).
CREATE TABLE IF NOT EXISTS publication_invalidations (
    publication_id INTEGER PRIMARY KEY REFERENCES publications(id) ON DELETE CASCADE,
    event_seq      INTEGER NOT NULL REFERENCES trace_events(seq) ON DELETE CASCADE,
    recorded_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_pub_invalidations_event
    ON publication_invalidations (event_seq);

CREATE TABLE IF NOT EXISTS trace_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # Writers take the write lock up front so concurrent uploads/publishes
    # serialize instead of failing with SQLITE_BUSY on commit.
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add trace columns to tables created by older releases."""
    additions = {
        "publications": [
            ("created_event_seq", "INTEGER"),
            (
                "creation_time_known",
                "INTEGER NOT NULL DEFAULT 1 CHECK (creation_time_known IN (0, 1))",
            ),
            ("invalidated_event_seq", "INTEGER"),
        ],
    }
    for table, columns in additions.items():
        present = _column_names(conn, table)
        for name, ddl in columns:
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _seed_trace_baseline(conn: sqlite3.Connection) -> None:
    """Capture the fragment state at upgrade time as the trace starting point.

    Runs exactly once (guarded by ``trace_meta``).  Every station-minute that
    has surviving data gets a ``baseline`` event and ``unknown_before = 1``:
    we know the state *now*, but the changes that produced it were never
    recorded.  Existing publications are attached to that baseline with
    ``creation_time_known = 0`` — we know they existed, but not when they were
    written, and the changes that staled them are equally unknown.
    """
    minutes = conn.execute(
        "SELECT station, minute_utc FROM fragments "
        "UNION "
        "SELECT station, minute_utc FROM publications "
        "ORDER BY station, minute_utc"
    ).fetchall()

    for key in minutes:
        station = key["station"]
        minute = key["minute_utc"]
        rows = conn.execute(
            "SELECT * FROM fragments WHERE station = ? AND minute_utc = ? "
            "ORDER BY fragment_no",
            (station, minute),
        ).fetchall()
        snapshot = fragment_snapshot(rows)
        current_hash = snapshot_hash(snapshot)
        conn.execute(
            "INSERT INTO trace_events (station, minute_utc, kind, snapshot_hash, "
            "prior_snapshot_hash, snapshot_json, unknown_before) "
            "VALUES (?, ?, 'baseline', ?, NULL, ?, 1)",
            (
                station,
                minute,
                current_hash,
                json.dumps(snapshot, separators=(",", ":")),
            ),
        )

        pubs = conn.execute(
            "SELECT * FROM publications WHERE station = ? AND minute_utc = ? "
            "ORDER BY id",
            (station, minute),
        ).fetchall()
        for pub in pubs:
            # Reconcile the legacy stale flag with the baseline snapshot; the
            # fragment set (and the set hash, which includes revisions) never
            # reverts, so a hash mismatch really means the record is outdated.
            stale = 0 if pub["fragment_set_hash"] == current_hash else 1
            conn.execute(
                "UPDATE publications SET created_event_seq = NULL, "
                "creation_time_known = 0, invalidated_event_seq = NULL, stale = ? "
                "WHERE id = ?",
                (stale, pub["id"]),
            )


def _migrate_trace(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        done = conn.execute(
            "SELECT value FROM trace_meta WHERE key = ?", (TRACE_META_KEY,)
        ).fetchone()
        if done is not None:
            conn.commit()
            return
        _seed_trace_baseline(conn)
        conn.execute(
            "INSERT INTO trace_meta (key, value) VALUES (?, ?)",
            (TRACE_META_KEY, TRACE_META_VALUE),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db(db_path: Path | None = None) -> None:
    """Create/upgrade the schema if needed (safe on every startup)."""
    path = db_path or DB_PATH
    conn = _connect(path)
    try:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        conn.commit()
        _migrate_trace(conn)
    finally:
        conn.close()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Yield a request-scoped connection; the service layer owns commits."""
    conn = _connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Hold one read transaction so every SELECT sees the same committed state.

    Without this, SQLite in WAL mode lets each statement take its own snapshot,
    so a multi-query history read could mix two different committed states.
    The transaction is deferred (read-only) and always rolled back.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()
