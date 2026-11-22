from dataclasses import dataclass
import sqlite3

from .postings import require_day


@dataclass(frozen=True, slots=True)
class ObservationRevision:
    day: str
    batch_id: str
    line_id: str
    revision: int
    zone: str
    litres: int


def require_litres(value: object) -> None:
    if type(value) is not int or value < 0:
        raise ValueError('litres must be a nonnegative integer')


def read_current_revision(
    db: sqlite3.Connection, day: str, batch_id: str, line_id: str,
) -> ObservationRevision | None:
    row = db.execute(
        '''SELECT revision, zone, litres FROM current_revisions
           WHERE day = ? AND batch_id = ? AND line_id = ?''',
        (day, batch_id, line_id),
    ).fetchone()
    if row is None:
        return None
    revision, zone, litres = row
    return ObservationRevision(day, batch_id, line_id, revision, zone, int(litres))


def require_current_revision(
    db: sqlite3.Connection, day: str, expected: ObservationRevision,
) -> ObservationRevision:
    require_day(day)
    if not isinstance(expected, ObservationRevision) or expected.day != day:
        raise ValueError('revision belongs to another day or is invalid')
    if type(expected.revision) is not int or type(expected.litres) is not int:
        raise ValueError('invalid revision values')
    current = read_current_revision(db, day, expected.batch_id, expected.line_id)
    if current is None or current != expected:
        raise ValueError('unknown or stale observation revision')
    return current


def save_revision(db: sqlite3.Connection, revision: ObservationRevision) -> None:
    db.execute(
        '''INSERT INTO current_revisions (day, batch_id, line_id, revision, zone, litres)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (day, batch_id, line_id) DO UPDATE SET
             revision = excluded.revision, zone = excluded.zone, litres = excluded.litres''',
        (revision.day, revision.batch_id, revision.line_id, revision.revision,
         revision.zone, str(revision.litres)),
    )
