from collections.abc import Sequence
import sqlite3

from journal.balances import ZONES
from journal.postings import require_label

from .register import InventoryRecord, read_inventory


def resolve_inventory_selection(
    db: sqlite3.Connection, day: str, inventory_ids: Sequence[str],
) -> tuple[InventoryRecord, ...]:
    if (not isinstance(inventory_ids, Sequence) or isinstance(inventory_ids, (str, bytes))
            or len(inventory_ids) != len(ZONES)):
        raise ValueError('selection requires one inventory for each zone')
    selected = {}
    for count_id in inventory_ids:
        require_label(count_id, 'count id')
        record = read_inventory(db, count_id)
        if record is None:
            raise ValueError(f'missing inventory {count_id}')
        if record.day != day:
            raise ValueError(f'inventory {count_id} belongs to another day')
        if record.zone not in ZONES or record.zone in selected:
            raise ValueError('selection has an invalid or repeated zone')
        selected[record.zone] = record
    return tuple(selected[zone] for zone in ZONES)
