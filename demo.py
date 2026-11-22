from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from intake import BatchRequest, ReadingInput, submit
from inventory import freeze
from issue import issue_pack
from journal import correct_litres, initialize_source, read_current_revision, transfer
from restore import initialize_isolated, read_day, receive_pack, restore_day

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


if __name__ == '__main__':
    main()
