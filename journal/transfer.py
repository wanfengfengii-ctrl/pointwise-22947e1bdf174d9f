from dataclasses import replace
import sqlite3

from .balances import require_zone
from .postings import Posting, ZoneEffect, append_posting
from .revisions import ObservationRevision, require_current_revision, require_litres, save_revision
from .store import source_transaction


def transfer(
    db: sqlite3.Connection, day: str, expected: ObservationRevision,
    destination: str, litres: int,
) -> Posting:
    require_zone(destination)
    require_litres(litres)
    with source_transaction(db):
        current = require_current_revision(db, day, expected)
        if current.zone == destination:
            raise ValueError('transfer requires a different destination zone')
        updated = replace(current, revision=current.revision + 1, zone=destination, litres=litres)
        posting = append_posting(
            db, day, 'transfer', current.batch_id, ((current.line_id, updated.revision),),
            (ZoneEffect(current.zone, -1, -current.litres), ZoneEffect(destination, 1, litres)),
        )
        save_revision(db, updated)
        return posting
