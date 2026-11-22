from collections.abc import Sequence
import sqlite3

from inventory.cuts import require_formal_cut, require_target_not_before_counts
from inventory.reconcile import resolve_inventory_selection
from journal.postings import require_label
from journal.store import source_transaction

from .codec import content_identity, encode_envelope
from .effects import export_window
from .manifests import build_manifest, evidence_rows, inventory_payload
from .register import IssuedPackage, read_issued_package, save_issued_package


def _manifest_payload(manifest) -> dict:
    payload = {
        'day': manifest.day, 'issue_id': manifest.issue_id,
        'start': manifest.start, 'target': manifest.target,
        'inventory': [
            {'zone': reference.zone, 'count_id': reference.count_id, 'cut': reference.cut,
             'origin': reference.origin, 'identity': reference.identity}
            for reference in manifest.inventory
        ],
    }
    if manifest.row_evidence:
        payload['row_evidence'] = True
    return payload


def issue_pack(
    db: sqlite3.Connection, day: str, issue_id: str, inventory_ids: Sequence[str], target: int,
    *, row_evidence: bool = False,
) -> IssuedPackage:
    require_label(issue_id, 'issue id')
    with source_transaction(db):
        require_formal_cut(db, day, target)
        if read_issued_package(db, issue_id) is not None:
            raise ValueError('issue id already exists')
        inventories = resolve_inventory_selection(db, day, inventory_ids)
        require_target_not_before_counts(target, inventories)
        if row_evidence and any(record.evidence is None for record in inventories):
            raise ValueError('selected inventory lacks frozen row evidence')
        manifest = build_manifest(day, issue_id, target, inventories, row_evidence=row_evidence)
        counts = [inventory_payload(record) for record in inventories]
        body = {
            'manifest': _manifest_payload(manifest),
            'counts': counts,
            'postings': export_window(db, day, manifest.start, target),
        }
        if row_evidence:
            body['row_evidence'] = [
                {'zone': record.zone, 'count_id': record.count_id, 'cut': record.cut,
                 'origin': record.origin, 'rows': evidence_rows(record)}
                for record in inventories
            ]
        package = IssuedPackage(issue_id, day, manifest.start, target,
                                content_identity(body), encode_envelope(body))
        save_issued_package(db, package)
        return package
