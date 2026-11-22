from journal.balances import require_zone
from journal.postings import require_day, require_label
from journal.revisions import require_litres

from .records import BatchRequest, ReadingInput


def validate_batch(request: BatchRequest) -> None:
    if not isinstance(request, BatchRequest):
        raise ValueError('expected a batch request')
    require_day(request.day)
    require_label(request.batch_id, 'batch id')
    if not isinstance(request.readings, tuple) or not request.readings:
        raise ValueError('readings must be a nonempty ordered tuple')
    seen = set()
    for row in request.readings:
        if not isinstance(row, ReadingInput):
            raise ValueError('invalid reading row')
        require_label(row.line_id, 'line id')
        require_zone(row.zone)
        require_litres(row.litres)
        if row.line_id in seen:
            raise ValueError('duplicate line id in batch')
        seen.add(row.line_id)
