from dataclasses import dataclass
import json
import sqlite3

from journal.replay import RowSnapshot


@dataclass(frozen=True, slots=True)
class InventoryRecord:
    day: str
    zone: str
    count_id: str
    cut: int
    rows: int
    litres: int
    origin: str
    evidence: tuple[RowSnapshot, ...] | None = None


def encode_evidence(evidence: tuple[RowSnapshot, ...]) -> str:
    return json.dumps([
        [row.batch_id, row.line_id, row.revision, row.zone, row.litres]
        for row in evidence
    ], ensure_ascii=True, separators=(',', ':'))


def decode_evidence(value: str | None) -> tuple[RowSnapshot, ...] | None:
    if value is None:
        return None
    rows = json.loads(value)
    return tuple(RowSnapshot(batch_id, line_id, revision, zone, litres)
                 for batch_id, line_id, revision, zone, litres in rows)


def save_inventory(db: sqlite3.Connection, record: InventoryRecord) -> None:
    db.execute(
        '''INSERT INTO inventories
           (count_id, day, zone, cut, rows, litres, origin, evidence_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (record.count_id, record.day, record.zone, record.cut, record.rows,
         str(record.litres), record.origin,
         None if record.evidence is None else encode_evidence(record.evidence)),
    )


def read_inventory(db: sqlite3.Connection, count_id: str) -> InventoryRecord | None:
    row = db.execute(
        '''SELECT day, zone, cut, rows, litres, origin, evidence_json
           FROM inventories WHERE count_id = ?''',
        (count_id,),
    ).fetchone()
    if row is None:
        return None
    day, zone, cut, rows, litres, origin, evidence_json = row
    return InventoryRecord(day, zone, count_id, cut, rows, int(litres), origin,
                           decode_evidence(evidence_json))
