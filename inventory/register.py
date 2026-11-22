from dataclasses import dataclass
import sqlite3


@dataclass(frozen=True, slots=True)
class InventoryRecord:
    day: str
    zone: str
    count_id: str
    cut: int
    rows: int
    litres: int
    origin: str


def save_inventory(db: sqlite3.Connection, record: InventoryRecord) -> None:
    db.execute(
        '''INSERT INTO inventories (count_id, day, zone, cut, rows, litres, origin)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (record.count_id, record.day, record.zone, record.cut, record.rows,
         str(record.litres), record.origin),
    )


def read_inventory(db: sqlite3.Connection, count_id: str) -> InventoryRecord | None:
    row = db.execute(
        'SELECT day, zone, cut, rows, litres, origin FROM inventories WHERE count_id = ?',
        (count_id,),
    ).fetchone()
    if row is None:
        return None
    day, zone, cut, rows, litres, origin = row
    return InventoryRecord(day, zone, count_id, cut, rows, int(litres), origin)
