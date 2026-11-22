from dataclasses import replace
import sqlite3

from .postings import Posting, ZoneEffect, append_posting
from .revisions import ObservationRevision, require_current_revision, require_litres, save_revision
from .store import source_transaction


def correct_litres(
    db: sqlite3.Connection, day: str, expected: ObservationRevision, litres: int,
) -> Posting:
    require_litres(litres)
    with source_transaction(db):
        current = require_current_revision(db, day, expected)
        updated = replace(current, revision=current.revision + 1, litres=litres)
        posting = append_posting(
            db, day, 'correction', current.batch_id, ((current.line_id, updated.revision),),
            (ZoneEffect(current.zone, 0, litres - current.litres),),
        )
        save_revision(db, updated)
        return posting
