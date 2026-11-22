from collections import defaultdict
import sqlite3

from journal.postings import ZoneEffect, append_posting
from journal.revisions import ObservationRevision, save_revision
from journal.store import source_transaction

from .checks import validate_batch
from .receipts import IntakeReceipt, read_intake_receipt, save_intake_receipt
from .records import BatchRequest, read_batch_request, save_batch_request


def submit(db: sqlite3.Connection, request: BatchRequest) -> IntakeReceipt:
    validate_batch(request)
    with source_transaction(db):
        prior = read_batch_request(db, request.day, request.batch_id)
        if prior is not None:
            if prior != request:
                raise ValueError('batch identity already binds another request')
            receipt = read_intake_receipt(db, request.day, request.batch_id)
            if receipt is None:
                raise RuntimeError('formal batch has no intake receipt')
            return receipt

        save_batch_request(db, request)
        totals = defaultdict(lambda: [0, 0])
        for reading in request.readings:
            totals[reading.zone][0] += 1
            totals[reading.zone][1] += reading.litres
            save_revision(db, ObservationRevision(
                request.day, request.batch_id, reading.line_id, 1,
                reading.zone, reading.litres,
            ))
        posting = append_posting(
            db, request.day, 'intake', request.batch_id,
            tuple((reading.line_id, 1) for reading in request.readings),
            tuple(ZoneEffect(zone, rows, litres)
                  for zone, (rows, litres) in sorted(totals.items())),
        )
        receipt = IntakeReceipt(request.day, request.batch_id, posting.seq, len(request.readings))
        save_intake_receipt(db, receipt)
        return receipt
