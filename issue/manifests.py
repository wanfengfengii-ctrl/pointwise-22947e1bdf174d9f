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
    row_evidence: bool = False


def inventory_payload(record: InventoryRecord) -> dict:
    # The directory carries the formal cut for this identified count payload.
    return {
        'day': record.day, 'zone': record.zone, 'count_id': record.count_id,
        'rows': record.rows, 'litres': record.litres, 'origin': record.origin,
    }


def evidence_rows(record: InventoryRecord) -> list[list]:
    if record.evidence is None:
        raise ValueError(f'inventory {record.count_id} has no row evidence')
    return [
        [row.batch_id, row.line_id, row.revision, row.zone, row.litres]
        for row in record.evidence
    ]


def identity_payload(count_payload: dict, rows: object) -> dict:
    """The V2 identified payload: the V1 count with its frozen row evidence."""
    return {'count': count_payload, 'evidence': rows}


def build_manifest(
    day: str, issue_id: str, target: int, inventories: tuple[InventoryRecord, ...],
    *, row_evidence: bool = False,
) -> Manifest:
    references = tuple(
        InventoryReference(
            record.zone, record.count_id, record.cut, record.origin,
            content_identity(identity_payload(inventory_payload(record), evidence_rows(record))
                             if row_evidence else inventory_payload(record)),
        )
        for record in inventories
    )
    return Manifest(day, issue_id, min(reference.cut for reference in references), target,
                    references, row_evidence)


def parse_manifest(value: object) -> Manifest:
    base = {'day', 'issue_id', 'start', 'target', 'inventory'}
    if type(value) is dict and value.keys() == base | {'row_evidence'}:
        base = base | {'row_evidence'}
    data = require_fields(value, base, 'manifest')
    require_day(data['day'])
    require_label(data['issue_id'], 'issue id')
    require_cut(data['start'])
    require_cut(data['target'])
    row_evidence = data.get('row_evidence', False)
    if type(row_evidence) is not bool:
        raise ValueError('invalid row evidence marker')
    if type(data['inventory']) is not list or len(data['inventory']) != len(ZONES):
        raise ValueError('manifest requires one reference for each zone')
    by_zone = {}
    count_ids = set()
    for item in data['inventory']:
        reference_fields = require_fields(item, {'zone', 'count_id', 'cut', 'origin', 'identity'},
                                          'inventory reference')
        require_zone(reference_fields['zone'])
        require_label(reference_fields['count_id'], 'count id')
        require_label(reference_fields['origin'], 'inventory origin')
        require_cut(reference_fields['cut'])
        require_identity(reference_fields['identity'])
        reference = InventoryReference(**reference_fields)
        if reference.zone in by_zone or reference.count_id in count_ids:
            raise ValueError('duplicate inventory reference')
        if reference.cut > data['target']:
            raise ValueError('target precedes an inventory cut')
        by_zone[reference.zone] = reference
        count_ids.add(reference.count_id)
    inventory = tuple(by_zone[zone] for zone in ZONES)
    if data['start'] != min(reference.cut for reference in inventory):
        raise ValueError('continuation does not start at the earliest inventory cut')
    return Manifest(data['day'], data['issue_id'], data['start'], data['target'], inventory,
                    row_evidence)


def parse_row_evidence(value: object, manifest: Manifest) -> dict[str, list[list]]:
    """Validate the V2 row evidence block and return rows per zone.

    Each block must name the actually selected count, its cut and origin, and
    carry one state per observation of that partition.  The cryptographic bind
    is completed by the caller, which recomputes the identified payload from
    the matching count payload and these rows.
    """
    if not manifest.row_evidence:
        raise ValueError('manifest is not a row evidence delivery')
    if type(value) is not list or len(value) != len(manifest.inventory):
        raise ValueError('row evidence is incomplete')
    expected = {reference.zone: reference for reference in manifest.inventory}
    result = {}
    for item in value:
        data = require_fields(item, {'zone', 'count_id', 'cut', 'origin', 'rows'},
                              'row evidence')
        require_zone(data['zone'])
        reference = expected.pop(data['zone'], None)
        if reference is None:
            raise ValueError('repeated or unselected row evidence zone')
        if (data['count_id'] != reference.count_id or data['cut'] != reference.cut
                or data['origin'] != reference.origin):
            raise ValueError(f'row evidence does not bind count {reference.count_id}')
        if type(data['rows']) is not list:
            raise ValueError('row evidence rows must be a list')
        rows = []
        seen = set()
        for entry in data['rows']:
            if (type(entry) is not list or len(entry) != 5
                    or type(entry[2]) is not int or type(entry[4]) is not int):
                raise ValueError('invalid row evidence entry')
            batch_id, line_id, revision, zone, litres = entry
            require_label(batch_id, 'batch id')
            require_label(line_id, 'line id')
            if revision < 1 or litres < 0 or zone != data['zone']:
                raise ValueError('invalid row evidence state')
            if (batch_id, line_id) in seen:
                raise ValueError('repeated observation in row evidence')
            seen.add((batch_id, line_id))
            rows.append([batch_id, line_id, revision, zone, litres])
        result[data['zone']] = rows
    if expected:
        raise ValueError('row evidence is incomplete')
    return result
