from collections.abc import Sequence
from dataclasses import asdict
import sqlite3

from inventory.cuts import require_formal_cut, require_target_not_before_counts
from inventory.reconcile import resolve_inventory_selection
from journal.postings import require_label
from journal.store import source_transaction

from .codec import content_identity, encode_envelope
from .effects import export_window
from .manifests import build_manifest, inventory_payload
from .register import IssuedPackage, read_issued_package, save_issued_package


def issue_pack(
    db: sqlite3.Connection, day: str, issue_id: str, inventory_ids: Sequence[str], target: int,
) -> IssuedPackage:
    require_label(issue_id, 'issue id')
    with source_transaction(db):
        require_formal_cut(db, day, target)
        if read_issued_package(db, issue_id) is not None:
            raise ValueError('issue id already exists')
        inventories = resolve_inventory_selection(db, day, inventory_ids)
        require_target_not_before_counts(target, inventories)
        manifest = build_manifest(day, issue_id, target, inventories)
        body = {
            'manifest': asdict(manifest),
            'counts': [inventory_payload(record) for record in inventories],
            'postings': export_window(db, day, manifest.start, target),
        }
        package = IssuedPackage(issue_id, day, manifest.start, target,
                                content_identity(body), encode_envelope(body))
        save_issued_package(db, package)
        return package
