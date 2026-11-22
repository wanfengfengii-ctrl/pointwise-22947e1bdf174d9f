from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from intake import BatchRequest, ReadingInput, submit
from inventory import freeze
from issue import issue_pack
from journal import correct_litres, initialize_source, read_current_revision, transfer
from restore import (
    initialize_isolated, read_day, read_row_review, receive_pack, restore_day, review_rows,
)

DAY = '2026-04-12'


def main() -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp)
        source_path = root / 'source.sqlite'
        isolated_path = root / 'isolated.sqlite'
        with closing(sqlite3.connect(source_path)) as source:
            initialize_source(source)
            first = submit(source, BatchRequest(DAY, 'north-station', (ReadingInput('gauge', 'east', 5),)))
            east = freeze(source, DAY, 'east', 'east-morning', first.posting_seq)
            moved = transfer(source, DAY, read_current_revision(source, DAY, 'north-station', 'gauge'),
                             'west', 7)
            corrected = correct_litres(source, DAY,
                                       read_current_revision(source, DAY, 'north-station', 'gauge'), 8)
            west = freeze(source, DAY, 'west', 'west-evening', corrected.seq)
            issued = issue_pack(source, DAY, 'shift-end', (east.count_id, west.count_id), corrected.seq)
        source_path.unlink()
        with closing(sqlite3.connect(isolated_path)) as isolated:
            initialize_isolated(isolated)
            receipt = receive_pack(isolated, 'night-arrival', issued.raw)
            result = restore_day(isolated, receipt.receive_id, corrected.seq, 'closed-day')
        with closing(sqlite3.connect(isolated_path)) as reopened:
            initialize_isolated(reopened)
            retrieved = read_day(reopened, 'closed-day')

    actual = {
        'balances': [[item.zone, item.rows, item.litres] for item in result.reconciliation.balances],
        'coverage': [[entry.seq, effect.zone, effect.covered, effect.count_id]
                     for entry in result.reconciliation.audit for effect in entry.effects],
    }
    expected = {
        'balances': [['east', 0, 0], ['west', 1, 8]],
        'coverage': [[2, 'east', False, 'east-morning'], [2, 'west', True, 'west-evening'],
                     [3, 'west', True, 'west-evening']],
    }
    downstream_expected = {'matches_original': True, 'restore_id': 'closed-day',
                           'receive_id': 'night-arrival', 'target': 3, 'matches_issued_identity': True}
    downstream_actual = {
        'matches_original': retrieved == result,
        'restore_id': retrieved.receipt.restore_id if retrieved else None,
        'receive_id': retrieved.receipt.receive_id if retrieved else None,
        'target': retrieved.receipt.target if retrieved else None,
        'matches_issued_identity': retrieved is not None and retrieved.receipt.content_identity == issued.identity,
    }
    if actual != expected or downstream_actual != downstream_expected:
        raise SystemExit('POINTWISE_BUSINESS_FAILURE t6.east_early ' + json.dumps({
            'expected': expected, 'actual': actual, 'downstream': downstream_actual,
        }, ensure_ascii=False, sort_keys=True))
    scenario = {
        'case_id': 't6.east_early',
        'input': {'day': DAY, 'counts': [[east.count_id, east.cut, east.rows, east.litres],
                                       [west.count_id, west.cut, west.rows, west.litres]],
                  'transfer_seq': moved.seq, 'target': corrected.seq, 'source_present': source_path.exists()},
        'expected': expected,
        'actual': actual,
        'downstream_expected': downstream_expected,
        'downstream_actual': downstream_actual,
        'code_paths': ['intake/batches.py', 'journal/transfer.py', 'journal/corrections.py',
                       'inventory/counts.py', 'issue/packages.py', 'restore/reconcile.py',
                       'restore/signoff.py', 'restore/history.py'],
        'oracle': '东区原盘点一条 5 L，补扣一条 5 L。西区盘点已含转入的 7 L 和补正的 1 L。重开按原恢复号核对同一来件和首执。',
    }
    print('POINTWISE_SCENARIO ' + json.dumps(scenario, ensure_ascii=False, sort_keys=True))
    row_demo()


def row_demo() -> None:
    """Same-day V2 handoffs: two 5 L rows swap zones and both report a move."""
    with TemporaryDirectory() as temp:
        root = Path(temp)
        source_path = root / 'source.sqlite'
        isolated_path = root / 'isolated.sqlite'
        with closing(sqlite3.connect(source_path)) as source:
            initialize_source(source)
            first = submit(source, BatchRequest(
                DAY, 'station', (ReadingInput('a', 'east', 5), ReadingInput('b', 'west', 5))))
            freeze(source, DAY, 'east', 'east-start', first.posting_seq, row_evidence=True)
            freeze(source, DAY, 'west', 'west-start', first.posting_seq, row_evidence=True)
            moved = transfer(source, DAY, read_current_revision(source, DAY, 'station', 'a'),
                             'west', 5)
            freeze(source, DAY, 'west', 'west-middle', moved.seq, row_evidence=True)
            moved_back = transfer(source, DAY, read_current_revision(source, DAY, 'station', 'b'),
                                  'east', 5)
            freeze(source, DAY, 'east', 'east-end', moved_back.seq, row_evidence=True)
            freeze(source, DAY, 'west', 'west-end', moved_back.seq, row_evidence=True)
            with source:
                source.execute('DELETE FROM postings WHERE day = ? AND seq <= ?',
                               (DAY, first.posting_seq))
            before = issue_pack(source, DAY, 'before', ('east-start', 'west-start'),
                                first.posting_seq, row_evidence=True)
            staggered = issue_pack(source, DAY, 'staggered', ('east-start', 'west-middle'),
                                   moved_back.seq, row_evidence=True)
            with source:
                source.execute('DELETE FROM postings WHERE day = ? AND seq <= ?',
                               (DAY, moved_back.seq))
            after = issue_pack(source, DAY, 'after', ('east-end', 'west-end'),
                               moved_back.seq, row_evidence=True)
        source_path.unlink()
        with closing(sqlite3.connect(isolated_path)) as isolated:
            initialize_isolated(isolated)
            for name, package in (('before', before), ('staggered', staggered),
                                  ('after', after)):
                receive_pack(isolated, name, package.raw)
            result = review_rows(isolated, 'before', 'after', 'row-close')
            staggered_result = review_rows(isolated, 'before', 'staggered', 'row-mid')
        with closing(sqlite3.connect(isolated_path)) as reopened:
            initialize_isolated(reopened)
            retrieved = read_row_review(reopened, 'row-close')

    rows = [[row['line_id'], row['before']['zone'], row['before']['litres'],
             row['after']['zone'], row['after']['litres'], row['changes']]
            for row in result['rows']]
    expected = [['a', 'east', 5, 'west', 5, ['moved']],
                ['b', 'west', 5, 'east', 5, ['moved']]]
    mid = [[row['line_id'], row['after']['zone'], row['after']['source']]
           for row in staggered_result['rows']]
    downstream = {'matches_original': retrieved == result,
                  'review_id': retrieved['review_id'] if retrieved else None,
                  'source_present': source_path.exists()}
    if rows != expected or mid != [['a', 'west', {'count_id': 'west-middle'}],
                                   ['b', 'east', {'seq': 3}]] \
            or downstream != {'matches_original': True, 'review_id': 'row-close',
                             'source_present': False}:
        raise SystemExit('POINTWISE_BUSINESS_FAILURE row-review ' + json.dumps({
            'rows': rows, 'staggered': mid, 'downstream': downstream,
        }, ensure_ascii=False, sort_keys=True))
    scenario = {
        'case_id': 'row-review.exchange',
        'input': {'day': DAY, 'before_target': before.target, 'after_target': after.target,
                  'staggered_target': staggered.target, 'source_present': source_path.exists()},
        'actual': {'rows': rows, 'staggered': mid},
        'downstream': downstream,
        'code_paths': ['inventory/counts.py', 'issue/packages.py', 'restore/rows.py'],
        'oracle': '东区甲行、西区乙行各 5 L 等值互换。两件都报迁区，两端各区仍一条 5 L。'
                  '错峰件的 a 可指 west-middle，b 由正式号 3 确立。断源重开后按复核号复领。',
    }
    print('POINTWISE_ROW_REVIEW ' + json.dumps(scenario, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
