"""Replay the formal source sequence into per-observation row states.

The row evidence captured when a partition inventory is frozen is derived
from the original batch lines plus the complete formal postings from the day
start through the inventory cut.  Postings are never inferred from current
readings: each step must agree with the recorded revision chain and with the
recorded zone effects.
"""
from dataclasses import dataclass
import sqlite3

from .balances import require_zone
from .postings import (
    Posting, ZoneEffect, posting_exists, read_postings, require_contiguous, require_cut,
    require_day,
)


@dataclass(frozen=True, slots=True)
class RowSnapshot:
    batch_id: str
    line_id: str
    revision: int
    zone: str
    litres: int


def _initial_rows(
    db: sqlite3.Connection, day: str, postings: tuple[Posting, ...],
) -> dict[tuple[str, str], dict]:
    """Seed revision 1 rows for batches whose intake is inside the window."""
    batched = {posting.batch_id for posting in postings if posting.kind == 'intake'}
    rows = {}
    for batch_id, line_id, zone, litres in db.execute(
        'SELECT batch_id, line_id, zone, litres FROM batch_lines WHERE day = ?', (day,),
    ):
        if batch_id not in batched:
            continue
        rows[(batch_id, line_id)] = {
            'revision': 1, 'zone': zone, 'litres': int(litres),
        }
    return rows


def _require_row(rows: dict, key: tuple[str, str]) -> dict:
    row = rows.get(key)
    if row is None:
        raise ValueError(f'posting references unknown observation {key[1]!r}')
    return row


def _split_transfer(effects: tuple[ZoneEffect, ...]) -> tuple[ZoneEffect, ZoneEffect]:
    if len(effects) != 2:
        raise ValueError('transfer requires both zone effects')
    outgoing = [effect for effect in effects if effect.rows == -1]
    incoming = [effect for effect in effects if effect.rows == 1]
    if (len(outgoing) != 1 or len(incoming) != 1
            or outgoing[0].zone == incoming[0].zone):
        raise ValueError('invalid transfer zone effects')
    return outgoing[0], incoming[0]


def apply_posting(rows: dict, posting: Posting) -> None:
    """Apply one formal posting to the working row map, enforcing its shape."""
    if posting.kind == 'intake':
        for line_id, revision in posting.lines:
            row = _require_row(rows, (posting.batch_id, line_id))
            if revision != 1 or row['revision'] != 1:
                raise ValueError('invalid intake revision')
        return
    if len(posting.lines) != 1:
        raise ValueError('amendment must reference one observation')
    line_id, revision = posting.lines[0]
    row = _require_row(rows, (posting.batch_id, line_id))
    if revision < 2 or revision != row['revision'] + 1:
        raise ValueError('inconsistent revision chain')
    if posting.kind == 'transfer':
        outgoing, incoming = _split_transfer(posting.effects)
        if outgoing.zone != row['zone'] or outgoing.litres != -row['litres']:
            raise ValueError('transfer does not match the outgoing row')
        row['zone'] = incoming.zone
        row['litres'] = incoming.litres
    elif posting.kind == 'correction':
        if len(posting.effects) != 1:
            raise ValueError('invalid correction posting')
        effect = posting.effects[0]
        if effect.zone != row['zone'] or effect.rows != 0:
            raise ValueError('correction does not match the row zone')
        updated = row['litres'] + effect.litres
        if updated < 0:
            raise ValueError('correction leaves a negative reading')
        row['litres'] = updated
    else:
        raise ValueError('unknown formal posting kind')
    row['revision'] = revision


def row_states_at_cut(
    db: sqlite3.Connection, day: str, zone: str, cut: int,
) -> tuple[RowSnapshot, ...]:
    """Return every observation of ``zone`` as of the formal ``cut``.

    The replay needs the complete postings from the day start through the
    cut; retained-out history makes the evidence unavailable.
    """
    require_day(day)
    require_zone(zone)
    require_cut(cut)
    if not posting_exists(db, day, cut):
        raise ValueError('unknown formal cut for day')
    postings = read_postings(db, day, 0, cut)
    require_contiguous(postings, 0, cut)
    rows = _initial_rows(db, day, postings)
    for posting in postings:
        if posting.day != day:
            raise ValueError('posting has wrong day')
        apply_posting(rows, posting)
    return tuple(
        RowSnapshot(batch_id, line_id, row['revision'], row['zone'], row['litres'])
        for (batch_id, line_id), row in sorted(rows.items())
        if row['zone'] == zone
    )
