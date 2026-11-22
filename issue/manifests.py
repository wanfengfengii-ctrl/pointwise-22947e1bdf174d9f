from dataclasses import dataclass

from inventory.register import InventoryRecord
from journal.balances import ZONES, require_zone
from journal.postings import require_cut, require_day, require_label

from .codec import content_identity, require_fields, require_identity


@dataclass(frozen=True, slots=True)
class InventoryReference:
    zone: str
    count_id: str
    cut: int
    origin: str
    identity: str


@dataclass(frozen=True, slots=True)
class Manifest:
    day: str
    issue_id: str
    start: int
    target: int
    inventory: tuple[InventoryReference, ...]


def inventory_payload(record: InventoryRecord) -> dict:
    # The directory carries the formal cut for this identified count payload.
    return {
        'day': record.day, 'zone': record.zone, 'count_id': record.count_id,
        'rows': record.rows, 'litres': record.litres, 'origin': record.origin,
    }


def build_manifest(
    day: str, issue_id: str, target: int, inventories: tuple[InventoryRecord, ...],
) -> Manifest:
    references = tuple(
        InventoryReference(record.zone, record.count_id, record.cut, record.origin,
                           content_identity(inventory_payload(record)))
        for record in inventories
    )
    return Manifest(day, issue_id, min(reference.cut for reference in references), target, references)


def parse_manifest(value: object) -> Manifest:
    data = require_fields(value, {'day', 'issue_id', 'start', 'target', 'inventory'}, 'manifest')
    require_day(data['day'])
    require_label(data['issue_id'], 'issue id')
    require_cut(data['start'])
    require_cut(data['target'])
    if type(data['inventory']) is not list or len(data['inventory']) != len(ZONES):
        raise ValueError('manifest requires one reference for each zone')
    by_zone = {}
    count_ids = set()
    for item in data['inventory']:
        fields = require_fields(item, {'zone', 'count_id', 'cut', 'origin', 'identity'},
                                'inventory reference')
        require_zone(fields['zone'])
        require_label(fields['count_id'], 'count id')
        require_label(fields['origin'], 'inventory origin')
        require_cut(fields['cut'])
        require_identity(fields['identity'])
        reference = InventoryReference(**fields)
        if reference.zone in by_zone or reference.count_id in count_ids:
            raise ValueError('duplicate inventory reference')
        if reference.cut > data['target']:
            raise ValueError('target precedes an inventory cut')
        by_zone[reference.zone] = reference
        count_ids.add(reference.count_id)
    inventory = tuple(by_zone[zone] for zone in ZONES)
    if data['start'] != min(reference.cut for reference in inventory):
        raise ValueError('continuation does not start at the earliest inventory cut')
    return Manifest(data['day'], data['issue_id'], data['start'], data['target'], inventory)
