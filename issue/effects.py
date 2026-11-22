from dataclasses import asdict
import sqlite3

from journal.balances import require_zone
from journal.postings import Posting, ZoneEffect, read_postings, require_contiguous, require_label

from .codec import require_fields


def export_window(db: sqlite3.Connection, day: str, start: int, target: int) -> list[dict]:
    postings = read_postings(db, day, start, target)
    require_contiguous(postings, start, target)
    return [asdict(posting) for posting in postings]


def _read_lines(value: object) -> tuple[tuple[str, int], ...]:
    if type(value) is not list or not value:
        raise ValueError('posting requires observation references')
    lines = []
    seen = set()
    for item in value:
        if type(item) is not list or len(item) != 2:
            raise ValueError('invalid observation reference')
        line_id, revision = item
        require_label(line_id, 'line id')
        if line_id in seen or type(revision) is not int or revision < 1:
            raise ValueError('invalid observation revision reference')
        seen.add(line_id)
        lines.append((line_id, revision))
    return tuple(lines)


def _read_effects(value: object) -> tuple[ZoneEffect, ...]:
    if type(value) is not list or not value:
        raise ValueError('posting requires at least one zone effect')
    effects = []
    seen = set()
    for item in value:
        data = require_fields(item, {'zone', 'rows', 'litres'}, 'zone effect')
        require_zone(data['zone'])
        if data['zone'] in seen or type(data['rows']) is not int or type(data['litres']) is not int:
            raise ValueError('invalid or repeated zone effect')
        seen.add(data['zone'])
        effects.append(ZoneEffect(**data))
    return tuple(effects)


def _validate_posting_shape(posting: Posting) -> None:
    effects = posting.effects
    if posting.kind == 'intake':
        if (any(effect.rows < 1 or effect.litres < 0 for effect in effects)
                or sum(effect.rows for effect in effects) != len(posting.lines)
                or any(revision != 1 for _, revision in posting.lines)):
            raise ValueError('invalid intake posting')
        return
    if len(posting.lines) != 1 or posting.lines[0][1] < 2:
        raise ValueError('amendment requires one revised observation')
    if posting.kind == 'transfer':
        if len(effects) != 2:
            raise ValueError('transfer requires both zone effects')
        outgoing, incoming = effects
        if outgoing.rows != -1 or outgoing.litres > 0 or incoming.rows != 1 or incoming.litres < 0:
            raise ValueError('invalid transfer posting')
        return
    if posting.kind != 'correction' or len(effects) != 1 or effects[0].rows != 0:
        raise ValueError('invalid correction posting')


def parse_window(value: object, day: str, start: int, target: int) -> tuple[Posting, ...]:
    if type(value) is not list or len(value) != target - start:
        raise ValueError('incomplete continuation window')
    postings = []
    for item in value:
        data = require_fields(item, {'day', 'seq', 'kind', 'batch_id', 'lines', 'effects'}, 'posting')
        if data['day'] != day or type(data['seq']) is not int:
            raise ValueError('posting has wrong day or invalid formal sequence')
        require_label(data['kind'], 'posting kind')
        require_label(data['batch_id'], 'batch id')
        posting = Posting(day, data['seq'], data['kind'], data['batch_id'],
                          _read_lines(data['lines']), _read_effects(data['effects']))
        _validate_posting_shape(posting)
        postings.append(posting)
    result = tuple(postings)
    require_contiguous(result, start, target)
    return result
