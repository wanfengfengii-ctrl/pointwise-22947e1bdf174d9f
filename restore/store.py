from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3

_ISOLATED_SCHEMA = '''
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS received_packages (
    receive_id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    identity TEXT NOT NULL,
    raw BLOB NOT NULL,
    UNIQUE (day, issue_id)
);
CREATE TABLE IF NOT EXISTS restore_runs (
    restore_id TEXT PRIMARY KEY,
    receive_id TEXT NOT NULL REFERENCES received_packages(receive_id),
    day TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    target INTEGER NOT NULL CHECK (target >= 0)
);
CREATE TABLE IF NOT EXISTS restore_balances (
    restore_id TEXT NOT NULL REFERENCES restore_runs(restore_id),
    zone TEXT NOT NULL,
    rows INTEGER NOT NULL CHECK (rows >= 0),
    litres TEXT NOT NULL,
    inventory_json TEXT NOT NULL,
    PRIMARY KEY (restore_id, zone)
);
CREATE TABLE IF NOT EXISTS restore_audit (
    restore_id TEXT NOT NULL REFERENCES restore_runs(restore_id),
    seq INTEGER NOT NULL CHECK (seq > 0),
    posting_json TEXT NOT NULL,
    PRIMARY KEY (restore_id, seq)
);
CREATE TABLE IF NOT EXISTS restore_receipts (
    restore_id TEXT PRIMARY KEY REFERENCES restore_runs(restore_id),
    content_identity TEXT NOT NULL
);
COMMIT;
'''


def initialize_isolated(db: sqlite3.Connection) -> None:
    if db.in_transaction:
        raise ValueError('isolated initialization requires an idle connection')
    db.execute('PRAGMA foreign_keys = ON')
    try:
        db.executescript(_ISOLATED_SCHEMA)
    except BaseException:
        db.rollback()
        raise


@contextmanager
def isolated_transaction(db: sqlite3.Connection, *, write: bool = True) -> Iterator[None]:
    db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise
