import sqlite3

from journal.balances import balance_at_cut, require_zone
from journal.postings import require_label
from journal.store import source_transaction

from .cuts import require_formal_cut
from .register import InventoryRecord, read_inventory, save_inventory


def freeze(db: sqlite3.Connection, day: str, zone: str, count_id: str, cut: int) -> InventoryRecord:
    require_zone(zone)
    require_label(count_id, 'count id')
    with source_transaction(db):
        require_formal_cut(db, day, cut)
        if read_inventory(db, count_id) is not None:
            raise ValueError('count id already exists')
        balance = balance_at_cut(db, day, zone, cut)
        record = InventoryRecord(day, zone, count_id, cut, balance.rows, balance.litres,
                                 f'inventory/{day}/{zone}/{count_id}')
        save_inventory(db, record)
        return record
