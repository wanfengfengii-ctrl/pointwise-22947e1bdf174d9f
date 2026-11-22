import sqlite3

from journal.balances import balance_at_cut, require_zone
from journal.postings import require_label
from journal.replay import row_states_at_cut
from journal.store import source_transaction

from .cuts import require_formal_cut
from .register import InventoryRecord, read_inventory, save_inventory


def freeze(
    db: sqlite3.Connection, day: str, zone: str, count_id: str, cut: int,
    *, row_evidence: bool = False,
) -> InventoryRecord:
    require_zone(zone)
    require_label(count_id, 'count id')
    with source_transaction(db):
        require_formal_cut(db, day, cut)
        if read_inventory(db, count_id) is not None:
            raise ValueError('count id already exists')
        balance = balance_at_cut(db, day, zone, cut)
        evidence = row_states_at_cut(db, day, zone, cut) if row_evidence else None
        if evidence is not None:
            # The frozen evidence must reproduce the frozen partition figures.
            if (len(evidence), sum(row.litres for row in evidence)) != (balance.rows, balance.litres):
                raise ValueError('row evidence does not match the partition count')
        record = InventoryRecord(day, zone, count_id, cut, balance.rows, balance.litres,
                                 f'inventory/{day}/{zone}/{count_id}', evidence)
        save_inventory(db, record)
        return record
