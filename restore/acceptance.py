from dataclasses import dataclass
import sqlite3

from issue.codec import decode_envelope
from journal.postings import require_day, require_label

from .store import isolated_transaction


@dataclass(frozen=True, slots=True)
class ReceivedPackage:
    receive_id: str
    day: str
    issue_id: str
    identity: str
    raw: bytes


def read_received_package(db: sqlite3.Connection, receive_id: str) -> ReceivedPackage | None:
    row = db.execute(
        'SELECT day, issue_id, identity, raw FROM received_packages WHERE receive_id = ?',
        (receive_id,),
    ).fetchone()
    return None if row is None else ReceivedPackage(receive_id, *row)


def receive_pack(db: sqlite3.Connection, receive_id: str, raw: bytes) -> ReceivedPackage:
    require_label(receive_id, 'receive id')
    envelope = decode_envelope(raw)
    header = envelope.body.get('manifest')
    if type(header) is not dict:
        raise ValueError('missing handoff header')
    day = header.get('day')
    issue_id = header.get('issue_id')
    require_day(day)
    require_label(issue_id, 'issue id')
    candidate = ReceivedPackage(receive_id, day, issue_id, envelope.identity, raw)
    with isolated_transaction(db):
        existing = read_received_package(db, receive_id)
        if existing is not None:
            if existing != candidate:
                raise ValueError('receive id already binds a different delivery')
            return existing
        owner = db.execute(
            'SELECT receive_id FROM received_packages WHERE day = ? AND issue_id = ?',
            (day, issue_id),
        ).fetchone()
        if owner is not None:
            raise ValueError('issued delivery is already bound to a receive id')
        db.execute(
            '''INSERT INTO received_packages (receive_id, day, issue_id, identity, raw)
               VALUES (?, ?, ?, ?, ?)''',
            (receive_id, day, issue_id, candidate.identity, raw),
        )
        return candidate
