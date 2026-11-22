from dataclasses import asdict, dataclass
from datetime import date
import json
import sqlite3


@dataclass(frozen=True, slots=True)
class ZoneEffect:
    zone: str
    rows: int
    litres: int


@dataclass(frozen=True, slots=True)
class Posting:
    day: str
    seq: int
    kind: str
    batch_id: str
    lines: tuple[tuple[str, int], ...]
    effects: tuple[ZoneEffect, ...]


def require_label(value: object, name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f'{name} must be nonempty text')
    value.encode('utf-8')


def require_day(value: object) -> None:
    require_label(value, 'day')
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError('day must use YYYY-MM-DD')


def require_cut(cut: object) -> None:
    if type(cut) is not int or cut < 0:
        raise ValueError('cut must be a nonnegative integer')


def append_posting(
    db: sqlite3.Connection, day: str, kind: str, batch_id: str,
    lines: tuple[tuple[str, int], ...], effects: tuple[ZoneEffect, ...],
) -> Posting:
    db.execute('INSERT OR IGNORE INTO source_days (day, last_seq) VALUES (?, 0)', (day,))
    seq = db.execute('SELECT last_seq FROM source_days WHERE day = ?', (day,)).fetchone()[0] + 1
    db.execute(
        '''INSERT INTO postings (day, seq, kind, batch_id, lines_json, effects_json)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (day, seq, kind, batch_id, json.dumps(lines),
         json.dumps([asdict(effect) for effect in effects])),
    )
    db.execute('UPDATE source_days SET last_seq = ? WHERE day = ?', (seq, day))
    return Posting(day, seq, kind, batch_id, lines, effects)


def read_postings(db: sqlite3.Connection, day: str, after: int, through: int) -> tuple[Posting, ...]:
    rows = db.execute(
        '''SELECT seq, kind, batch_id, lines_json, effects_json FROM postings
           WHERE day = ? AND seq > ? AND seq <= ? ORDER BY seq''',
        (day, after, through),
    )
    return tuple(
        Posting(day, seq, kind, batch_id,
                tuple(tuple(line) for line in json.loads(lines)),
                tuple(ZoneEffect(**effect) for effect in json.loads(effects)))
        for seq, kind, batch_id, lines, effects in rows
    )


def posting_exists(db: sqlite3.Connection, day: str, cut: int) -> bool:
    require_cut(cut)
    head = db.execute('SELECT last_seq FROM source_days WHERE day = ?', (day,)).fetchone()
    return head is not None and cut <= head[0]


def require_contiguous(postings: tuple[Posting, ...], after: int, through: int) -> None:
    if through < after or len(postings) != through - after:
        raise ValueError('incomplete continuation window')
    for seq, posting in enumerate(postings, after + 1):
        if posting.seq != seq:
            raise ValueError(f'expected formal posting {seq}')
