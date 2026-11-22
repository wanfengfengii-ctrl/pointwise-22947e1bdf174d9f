from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from intake import BatchRequest, ReadingInput, read_intake_receipt, submit
from intake.records import read_batch_request
from journal import balance_at_cut, correct_litres, initialize_source, read_current_revision, read_postings

DAY = '2026-04-12'


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(closing(sqlite3.connect(':memory:')))
        initialize_source(self.db)
        self.request = BatchRequest(DAY, 'station-a', (
            ReadingInput('r1', 'east', 5), ReadingInput('r2', 'west', 0),
        ))

    def test_t1_replay_and_changed_value(self):
        first = submit(self.db, self.request)
        self.assertEqual((first.day, first.batch_id, first.posting_seq, first.row_count),
                         (DAY, 'station-a', 1, 2))
        self.assertEqual(submit(self.db, self.request), first)
        changed = BatchRequest(DAY, 'station-a', (ReadingInput('r1', 'east', 6), self.request.readings[1]))
        with self.assertRaisesRegex(ValueError, 'another request'):
            submit(self.db, changed)
        self.assertEqual(read_intake_receipt(self.db, DAY, 'station-a'), first)
        self.assertEqual(read_batch_request(self.db, DAY, 'station-a'), self.request)
        self.assertEqual([posting.seq for posting in read_postings(self.db, DAY, 0, 10)], [1])
        west = balance_at_cut(self.db, DAY, 'west', 1)
        self.assertEqual((west.rows, west.litres), (1, 0))

    def test_t1_ordered_replay(self):
        first = submit(self.db, self.request)
        reversed_request = BatchRequest(DAY, 'station-a', tuple(reversed(self.request.readings)))
        with self.assertRaisesRegex(ValueError, 'another request'):
            submit(self.db, reversed_request)
        self.assertEqual(read_batch_request(self.db, DAY, 'station-a'), self.request)
        self.assertEqual(submit(self.db, self.request), first)

    def test_t1_same_value_distinct_batches(self):
        first = submit(self.db, self.request)
        second = submit(self.db, BatchRequest(DAY, 'station-b', self.request.readings))
        self.assertEqual((first.posting_seq, second.posting_seq), (1, 2))
        east = balance_at_cut(self.db, DAY, 'east', 2)
        self.assertEqual((east.rows, east.litres), (2, 10))
        self.assertEqual(read_current_revision(self.db, DAY, 'station-b', 'r1').litres, 5)

    def test_t1_invalid_row_atomic(self):
        invalid = (None, ReadingInput('bad', 'west', None), ReadingInput('bad', 'west', True),
                   ReadingInput('bad', 'west', 1.5), ReadingInput('bad', 'west', '5'),
                   ReadingInput('bad', 'west', -1), ReadingInput('bad', 'north', 4),
                   ReadingInput('r1', 'west', 2))
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                submit(self.db, BatchRequest(DAY, 'station-a', (self.request.readings[0], row)))
        self.assertIsNone(read_batch_request(self.db, DAY, 'station-a'))
        self.assertIsNone(read_intake_receipt(self.db, DAY, 'station-a'))
        self.assertIsNone(read_current_revision(self.db, DAY, 'station-a', 'r1'))
        self.assertEqual(read_postings(self.db, DAY, 0, 10), ())
        self.assertEqual(submit(self.db, self.request).posting_seq, 1)

    def test_t1_receipt_write_failure_rolls_back(self):
        self.db.executescript('''
            CREATE TRIGGER reject_intake_receipt BEFORE INSERT ON intake_receipts
            BEGIN SELECT RAISE(ABORT, 'intake receipt rejected'); END;
        ''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'intake receipt rejected'):
            submit(self.db, self.request)
        self.assertIsNone(read_batch_request(self.db, DAY, 'station-a'))
        self.assertIsNone(read_current_revision(self.db, DAY, 'station-a', 'r1'))
        self.assertIsNone(read_intake_receipt(self.db, DAY, 'station-a'))
        self.assertEqual(read_postings(self.db, DAY, 0, 10), ())
        self.db.execute('DROP TRIGGER reject_intake_receipt')
        self.assertEqual(submit(self.db, self.request).posting_seq, 1)

    def test_t1_reinitialize(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'source.sqlite'
            with closing(sqlite3.connect(path)) as db:
                initialize_source(db)
                first = submit(db, self.request)
                correct_litres(db, DAY, read_current_revision(db, DAY, 'station-a', 'r1'), 8)
            with closing(sqlite3.connect(path)) as db:
                initialize_source(db)
                initialize_source(db)
                self.assertEqual(submit(db, self.request), first)
                self.assertEqual(read_batch_request(db, DAY, 'station-a'), self.request)
                current = read_current_revision(db, DAY, 'station-a', 'r1')
                self.assertEqual((current.revision, current.litres), (2, 8))
                self.assertEqual([entry.seq for entry in read_postings(db, DAY, 0, 10)], [1, 2])
