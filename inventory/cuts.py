import sqlite3

from journal.postings import posting_exists, require_day

from .register import InventoryRecord


def require_formal_cut(db: sqlite3.Connection, day: str, cut: int) -> None:
    require_day(day)
    if not posting_exists(db, day, cut):
        raise ValueError('unknown formal cut for day')


def require_target_not_before_counts(target: int, inventories: tuple[InventoryRecord, ...]) -> None:
    for inventory in inventories:
        if inventory.cut > target:
            raise ValueError(f'target precedes inventory {inventory.count_id}')
