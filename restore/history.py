import sqlite3

from journal.postings import require_label

from .signoff import RestoredDay, _load_restored_day
from .store import isolated_transaction


def read_day(db: sqlite3.Connection, restore_id: str) -> RestoredDay | None:
    require_label(restore_id, 'restore id')
    with isolated_transaction(db, write=False):
        return _load_restored_day(db, restore_id)
