from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from intake import BatchRequest, ReadingInput, submit
from inventory import freeze
from issue import issue_pack
from issue.codec import decode_envelope, encode_envelope
from journal import correct_litres, initialize_source, read_current_revision, transfer
from restore import initialize_isolated, read_day, read_received_package, receive_pack, restore_day

DAY = '2026-04-12'


def figures(result):
    return [(balance.zone, balance.rows, balance.litres) for balance in result.reconciliation.balances]


def coverage(result):
    return [(posting.seq, effect.zone, effect.covered, effect.count_id, effect.cut)
            for posting in result.reconciliation.audit for effect in posting.effects]


class RestoreTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.source_path = directory / 'source.sqlite'
        self.path = directory / 'isolated.sqlite'
        with closing(sqlite3.connect(self.source_path)) as source:
            initialize_source(source)
            submit(source, BatchRequest(DAY, 'station', (ReadingInput('r1', 'east', 5),)))
            freeze(source, DAY, 'east', 'east-early', 1)
            freeze(source, DAY, 'west', 'west-early', 1)
            freeze(source, DAY, 'west', 'west-equal', 1)
            transfer(source, DAY, read_current_revision(source, DAY, 'station', 'r1'), 'west', 7)
            correct_litres(source, DAY, read_current_revision(source, DAY, 'station', 'r1'), 8)
            freeze(source, DAY, 'east', 'east-late', 3)
            freeze(source, DAY, 'west', 'west-late', 3)
            correct_litres(source, DAY, read_current_revision(source, DAY, 'station', 'r1'), 8)
            correct_litres(source, DAY, read_current_revision(source, DAY, 'station', 'r1'), 9)
            # Simulate loss of source history at and before the earliest selected cut.
            with source:
                source.execute('DELETE FROM postings WHERE day = ? AND seq <= 1', (DAY,))
            self.same = issue_pack(source, DAY, 'same', ('east-early', 'west-early'), 3)
            self.east_first = issue_pack(source, DAY, 'east-first', ('east-early', 'west-late'), 3)
            self.west_first = issue_pack(source, DAY, 'west-first', ('east-late', 'west-early'), 3)
            self.other = issue_pack(source, DAY, 'other', ('east-early', 'west-equal'), 3)
            self.full = issue_pack(source, DAY, 'full', ('east-early', 'west-early'), 5)
            self.empty = issue_pack(source, DAY, 'empty', ('east-late', 'west-late'), 3)
        self.source_path.unlink()
        self.db = self.enterContext(closing(sqlite3.connect(self.path)))
        initialize_isolated(self.db)

    def assert_empty_signoff(self, db):
        self.assertEqual(db.execute('SELECT count(*) FROM restore_runs').fetchone(), (0,))
        self.assertEqual(db.execute('SELECT count(*) FROM restore_balances').fetchone(), (0,))
        self.assertEqual(db.execute('SELECT count(*) FROM restore_audit').fetchone(), (0,))
        self.assertEqual(db.execute('SELECT count(*) FROM restore_receipts').fetchone(), (0,))

    def test_t5_reopened_history(self):
        self.assertFalse(self.source_path.exists())
        arrival = receive_pack(self.db, 'night', self.same.raw)
        self.assertEqual(receive_pack(self.db, 'night', self.same.raw), arrival)
        first = restore_day(self.db, 'night', 3, 'first')
        self.assertEqual(figures(first), [('east', 0, 0), ('west', 1, 8)])
        self.assertEqual(coverage(first), [(2, 'east', False, 'east-early', 1),
                                           (2, 'west', False, 'west-early', 1),
                                           (3, 'west', False, 'west-early', 1)])
        self.assertEqual((first.receipt.restore_id, first.receipt.receive_id, first.receipt.day,
                          first.receipt.issue_id, first.receipt.target, first.receipt.content_identity),
                         ('first', 'night', DAY, 'same', 3, self.same.identity))
        self.assertEqual(first.reconciliation.audit[0].effects[1].origin,
                         f'inventory/{DAY}/west/west-early')
        receive_pack(self.db, 'later', self.east_first.raw)
        second = restore_day(self.db, 'later', 3, 'second')
        self.assertEqual(figures(second), figures(first))
        self.assertNotEqual(second.receipt.content_identity, first.receipt.content_identity)
        with self.assertRaisesRegex(ValueError, 'restore id already binds'):
            restore_day(self.db, 'later', 3, 'first')
        self.db.close()
        with closing(sqlite3.connect(self.path)) as reopened:
            initialize_isolated(reopened)
            self.assertEqual(read_day(reopened, 'first'), first)
            self.assertEqual(read_day(reopened, 'second'), second)
            self.assertEqual(restore_day(reopened, 'night', 3, 'first'), first)
            self.assertEqual(read_received_package(reopened, 'night'), arrival)
            self.assertIsNone(read_day(reopened, 'unknown'))

    def test_t5_receive_conflict(self):
        original = receive_pack(self.db, 'night', self.same.raw)
        with self.assertRaisesRegex(ValueError, 'receive id already binds'):
            receive_pack(self.db, 'night', self.other.raw)
        with self.assertRaisesRegex(ValueError, 'already bound'):
            receive_pack(self.db, 'another', self.same.raw)
        body = decode_envelope(self.same.raw).body
        body['postings'].pop()
        changed = encode_envelope(body)
        with self.assertRaisesRegex(ValueError, 'already bound'):
            receive_pack(self.db, 'another', changed)
        raw = self.same.raw.replace(self.same.identity.encode(), b'0' * 64)
        with self.assertRaisesRegex(ValueError, 'content identity mismatch'):
            receive_pack(self.db, 'bad', raw)
        self.assertEqual(read_received_package(self.db, 'night'), original)
        self.assertIsNone(read_received_package(self.db, 'another'))
        self.assertIsNone(read_received_package(self.db, 'bad'))

    def test_t5_equal_amount_substitution(self):
        body = decode_envelope(self.same.raw).body
        alternate = decode_envelope(self.other.raw).body['counts'][1]
        original = body['counts'][1]
        self.assertEqual((original['rows'], original['litres']), (alternate['rows'], alternate['litres']))
        self.assertNotEqual(original['count_id'], alternate['count_id'])
        body['counts'][1] = alternate
        receive_pack(self.db, 'night', encode_envelope(body))
        with self.assertRaisesRegex(ValueError, 'unselected or repeated inventory'):
            restore_day(self.db, 'night', 3, 'bad')
        self.assertIsNone(read_day(self.db, 'bad'))
        self.assert_empty_signoff(self.db)

    def test_t5_empty_window(self):
        receive_pack(self.db, 'empty', self.empty.raw)
        result = restore_day(self.db, 'empty', 3, 'close')
        self.assertEqual(figures(result), [('east', 0, 0), ('west', 1, 8)])
        self.assertEqual(result.reconciliation.audit, ())
        self.assertEqual([(ref.count_id, ref.cut) for ref in result.reconciliation.counts],
                         [('east-late', 3), ('west-late', 3)])

    def test_t6_east_early(self):
        receive_pack(self.db, 'night', self.east_first.raw)
        result = restore_day(self.db, 'night', 3, 'close')
        self.assertEqual(figures(result), [('east', 0, 0), ('west', 1, 8)])
        self.assertEqual(coverage(result), [(2, 'east', False, 'east-early', 1),
                                            (2, 'west', True, 'west-late', 3),
                                            (3, 'west', True, 'west-late', 3)])
        transfer_audit = result.reconciliation.audit[0]
        self.assertEqual((transfer_audit.kind, transfer_audit.batch_id, transfer_audit.lines),
                         ('transfer', 'station', (('r1', 2),)))
        self.assertEqual([(effect.rows, effect.litres) for effect in transfer_audit.effects],
                         [(-1, -5), (1, 7)])
        reference = result.reconciliation.counts[1]
        self.assertEqual((transfer_audit.effects[1].count_identity, transfer_audit.effects[1].origin),
                         (reference.identity, f'inventory/{DAY}/west/west-late'))
        self.assertEqual(read_day(self.db, 'close'), result)

    def test_t6_west_early(self):
        receive_pack(self.db, 'night', self.west_first.raw)
        result = restore_day(self.db, 'night', 3, 'close')
        self.assertEqual(figures(result), [('east', 0, 0), ('west', 1, 8)])
        self.assertEqual(coverage(result), [(2, 'east', True, 'east-late', 3),
                                            (2, 'west', False, 'west-early', 1),
                                            (3, 'west', False, 'west-early', 1)])

    def test_t7_zero_entry_present(self):
        receive_pack(self.db, 'night', self.full.raw)
        result = restore_day(self.db, 'night', 5, 'close')
        self.assertEqual(figures(result), [('east', 0, 0), ('west', 1, 9)])
        self.assertEqual([posting.seq for posting in result.reconciliation.audit], [2, 3, 4, 5])
        zero = result.reconciliation.audit[2]
        self.assertEqual((zero.kind, zero.batch_id, zero.lines), ('correction', 'station', (('r1', 4),)))
        self.assertEqual([(effect.zone, effect.rows, effect.litres, effect.covered) for effect in zero.effects],
                         [('west', 0, 0, False)])
        self.assertEqual(read_day(self.db, 'close'), result)

    def test_t7_missing_zero_entry(self):
        body = decode_envelope(self.full.raw).body
        del body['postings'][2]
        arrival = receive_pack(self.db, 'night', encode_envelope(body))
        with self.assertRaisesRegex(ValueError, 'incomplete continuation'):
            restore_day(self.db, 'night', 5, 'bad')
        self.assertIsNone(read_day(self.db, 'bad'))
        self.assertEqual(read_received_package(self.db, 'night'), arrival)
        self.assert_empty_signoff(self.db)

    def test_t7_wrong_day_missing_count_wrong_target_and_wrong_sequence(self):
        for case in ('day', 'missing', 'request-target', 'manifest-target', 'sequence'):
            with self.subTest(case=case):
                body = decode_envelope(self.east_first.raw).body
                body['manifest']['issue_id'] = case
                target = 3
                if case == 'day':
                    body['postings'][0]['day'] = '2026-04-13'
                elif case == 'missing':
                    body['counts'].pop()
                elif case == 'request-target':
                    target = 2
                elif case == 'manifest-target':
                    body['manifest']['target'] = target = 2
                else:
                    body['postings'][0]['seq'] = 3
                receive_pack(self.db, case, encode_envelope(body))
                with self.assertRaises(ValueError):
                    restore_day(self.db, case, target, case)
                self.assertIsNone(read_day(self.db, case))
        self.assert_empty_signoff(self.db)

    def test_t8_rollback_then_retry_and_reinitialize(self):
        arrival = receive_pack(self.db, 'night', self.east_first.raw)
        self.db.executescript('''
            CREATE TRIGGER reject_signoff BEFORE INSERT ON restore_receipts
            WHEN NEW.restore_id = 'close'
            BEGIN
                SELECT CASE WHEN
                    (SELECT count(*) FROM restore_balances WHERE restore_id = NEW.restore_id) = 2
                    AND (SELECT count(*) FROM restore_audit WHERE restore_id = NEW.restore_id) = 2
                    THEN RAISE(ABORT, 'receipt rejected after complete writes')
                    ELSE RAISE(ABORT, 'receipt attempted before complete writes')
                END;
            END;
        ''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'receipt rejected after complete writes'):
            restore_day(self.db, 'night', 3, 'close')
        self.db.close()
        with closing(sqlite3.connect(self.path)) as reopened:
            initialize_isolated(reopened)
            self.assertIsNone(read_day(reopened, 'close'))
            self.assert_empty_signoff(reopened)
            self.assertEqual(read_received_package(reopened, 'night'), arrival)
            reopened.execute('DROP TRIGGER reject_signoff')
            result = restore_day(reopened, 'night', 3, 'close')
            self.assertEqual(figures(result), [('east', 0, 0), ('west', 1, 8)])
            self.assertEqual(coverage(result), [(2, 'east', False, 'east-early', 1),
                                                (2, 'west', True, 'west-late', 3),
                                                (3, 'west', True, 'west-late', 3)])
            self.assertEqual(reopened.execute('SELECT count(*) FROM restore_receipts').fetchone(), (1,))
        with closing(sqlite3.connect(self.path)) as reopened:
            initialize_isolated(reopened)
            self.assertEqual(read_day(reopened, 'close'), result)
            self.assertEqual(restore_day(reopened, 'night', 3, 'close'), result)
            self.assertEqual(result.receipt.content_identity, self.east_first.identity)
