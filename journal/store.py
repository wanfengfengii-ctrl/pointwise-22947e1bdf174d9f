from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3

_SOURCE_SCHEMA = '''
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS source_days (
    day TEXT PRIMARY KEY,
    last_seq INTEGER NOT NULL CHECK (last_seq >= 0)
);
CREATE TABLE IF NOT EXISTS postings (
    day TEXT NOT NULL REFERENCES source_days(day),
    seq INTEGER NOT NULL CHECK (seq > 0),
    kind TEXT NOT NULL CHECK (kind IN ('intake', 'transfer', 'correction')),
    batch_id TEXT NOT NULL,
    lines_json TEXT NOT NULL,
    effects_json TEXT NOT NULL,
    PRIMARY KEY (day, seq)
);
CREATE TABLE IF NOT EXISTS batch_requests (
    day TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    PRIMARY KEY (day, batch_id)
);
CREATE TABLE IF NOT EXISTS batch_lines (
    day TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    line_id TEXT NOT NULL,
    zone TEXT NOT NULL,
    litres TEXT NOT NULL,
    PRIMARY KEY (day, batch_id, line_id),
    UNIQUE (day, batch_id, position),
    FOREIGN KEY (day, batch_id) REFERENCES batch_requests(day, batch_id)
);
CREATE TABLE IF NOT EXISTS current_revisions (
    day TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    zone TEXT NOT NULL,
    litres TEXT NOT NULL,
    PRIMARY KEY (day, batch_id, line_id),
    FOREIGN KEY (day, batch_id, line_id) REFERENCES batch_lines(day, batch_id, line_id)
);
CREATE TABLE IF NOT EXISTS intake_receipts (
    day TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    posting_seq INTEGER NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count > 0),
    PRIMARY KEY (day, batch_id),
    FOREIGN KEY (day, batch_id) REFERENCES batch_requests(day, batch_id)
);
CREATE TABLE IF NOT EXISTS inventories (
    count_id TEXT PRIMARY KEY,
    day TEXT NOT NULL REFERENCES source_days(day),
    zone TEXT NOT NULL,
    cut INTEGER NOT NULL CHECK (cut >= 0),
    rows INTEGER NOT NULL CHECK (rows >= 0),
    litres TEXT NOT NULL,
    origin TEXT NOT NULL,
    evidence_json TEXT
);
CREATE TABLE IF NOT EXISTS issued_packages (
    issue_id TEXT PRIMARY KEY,
    day TEXT NOT NULL REFERENCES source_days(day),
    start INTEGER NOT NULL CHECK (start >= 0),
    target INTEGER NOT NULL CHECK (target >= start),
    identity TEXT NOT NULL,
    raw BLOB NOT NULL
);
COMMIT;
'''


def _upgrade(db: sqlite3.Connection) -> None:
    """Add columns introduced after the original schema, keeping old data."""
    columns = {row[1] for row in db.execute('PRAGMA table_info(inventories)')}
    if 'evidence_json' not in columns:
        db.execute('ALTER TABLE inventories ADD COLUMN evidence_json TEXT')


def initialize_source(db: sqlite3.Connection) -> None:
    if db.in_transaction:
        raise ValueError('source initialization requires an idle connection')
    db.execute('PRAGMA foreign_keys = ON')
    try:
        db.executescript(_SOURCE_SCHEMA)
        _upgrade(db)
    except BaseException:
        db.rollback()
        raise


@contextmanager
def source_transaction(db: sqlite3.Connection) -> Iterator[None]:
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise
