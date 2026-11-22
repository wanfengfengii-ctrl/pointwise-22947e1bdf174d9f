from dataclasses import dataclass
import sqlite3


@dataclass(frozen=True, slots=True)
class ReadingInput:
    line_id: str
    zone: str
    litres: int


@dataclass(frozen=True, slots=True)
class BatchRequest:
    day: str
    batch_id: str
    readings: tuple[ReadingInput, ...]


def save_batch_request(db: sqlite3.Connection, request: BatchRequest) -> None:
    db.execute('INSERT INTO batch_requests (day, batch_id) VALUES (?, ?)',
               (request.day, request.batch_id))
    db.executemany(
        '''INSERT INTO batch_lines (day, batch_id, position, line_id, zone, litres)
           VALUES (?, ?, ?, ?, ?, ?)''',
        ((request.day, request.batch_id, position, row.line_id, row.zone, str(row.litres))
         for position, row in enumerate(request.readings)),
    )


def read_batch_request(db: sqlite3.Connection, day: str, batch_id: str) -> BatchRequest | None:
    if db.execute('SELECT 1 FROM batch_requests WHERE day = ? AND batch_id = ?',
                  (day, batch_id)).fetchone() is None:
        return None
    rows = db.execute(
        '''SELECT line_id, zone, litres FROM batch_lines
           WHERE day = ? AND batch_id = ? ORDER BY position''',
        (day, batch_id),
    )
    return BatchRequest(day, batch_id, tuple(
        ReadingInput(line_id, zone, int(litres)) for line_id, zone, litres in rows
    ))
