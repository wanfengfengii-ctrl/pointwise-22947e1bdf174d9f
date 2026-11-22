from dataclasses import dataclass
import sqlite3


@dataclass(frozen=True, slots=True)
class IntakeReceipt:
    day: str
    batch_id: str
    posting_seq: int
    row_count: int


def save_intake_receipt(db: sqlite3.Connection, receipt: IntakeReceipt) -> None:
    db.execute(
        '''INSERT INTO intake_receipts (day, batch_id, posting_seq, row_count)
           VALUES (?, ?, ?, ?)''',
        (receipt.day, receipt.batch_id, receipt.posting_seq, receipt.row_count),
    )


def read_intake_receipt(db: sqlite3.Connection, day: str, batch_id: str) -> IntakeReceipt | None:
    row = db.execute(
        'SELECT posting_seq, row_count FROM intake_receipts WHERE day = ? AND batch_id = ?',
        (day, batch_id),
    ).fetchone()
    return None if row is None else IntakeReceipt(day, batch_id, *row)
