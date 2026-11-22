from contextlib import closing
import sqlite3
import unittest

from intake import BatchRequest, ReadingInput, submit
from journal import balance_at_cut, initialize_source, read_current_revision, read_postings, transfer

DAY = '2026-04-12'
OTHER_DAY = '2026-04-13'


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(closing(sqlite3.connect(':memory:')))
        initialize_source(self.db)
        submit(self.db, BatchRequest(DAY, 'station', (ReadingInput('r1', 'east', 5),)))
        self.original = read_current_revision(self.db, DAY, 'station', 'r1')

    def test_t2_transfer_amounts(self):
        posted = transfer(self.db, DAY, self.original, 'west', 7)
        self.assertEqual((posted.day, posted.seq, posted.kind, posted.batch_id, posted.lines),
                         (DAY, 2, 'transfer', 'station', (('r1', 2),)))
        self.assertEqual([(effect.zone, effect.rows, effect.litres) for effect in posted.effects],
                         [('east', -1, -5), ('west', 1, 7)])
        east = balance_at_cut(self.db, DAY, 'east', 2)
        west = balance_at_cut(self.db, DAY, 'west', 2)
        self.assertEqual((east.rows, east.litres, west.rows, west.litres), (0, 0, 1, 7))
        self.assertEqual(read_postings(self.db, DAY, 1, 2), (posted,))
        current = read_current_revision(self.db, DAY, 'station', 'r1')
        self.assertEqual((current.revision, current.zone, current.litres), (2, 'west', 7))

    def test_t2_old_revision(self):
        transfer(self.db, DAY, self.original, 'west', 7)
        with self.assertRaisesRegex(ValueError, 'stale'):
            transfer(self.db, DAY, self.original, 'east', 12)
        current = read_current_revision(self.db, DAY, 'station', 'r1')
        self.assertEqual((current.zone, current.litres, current.revision), ('west', 7, 2))
        self.assertEqual([entry.seq for entry in read_postings(self.db, DAY, 0, 10)], [1, 2])
        self.assertEqual(balance_at_cut(self.db, DAY, 'east', 2).litres, 0)
        self.assertEqual(balance_at_cut(self.db, DAY, 'west', 2).litres, 7)

    def test_t2_cross_day(self):
        submit(self.db, BatchRequest(OTHER_DAY, 'station', (ReadingInput('r1', 'east', 5),)))
        with self.assertRaisesRegex(ValueError, 'another day'):
            transfer(self.db, OTHER_DAY, self.original, 'west', 7)
        for day in (DAY, OTHER_DAY):
            self.assertEqual([entry.seq for entry in read_postings(self.db, day, 0, 10)], [1])
            self.assertEqual(balance_at_cut(self.db, day, 'east', 1).litres, 5)
            self.assertEqual(balance_at_cut(self.db, day, 'west', 1).litres, 0)

    def test_t2_revision_failure_rolls_back_both_sides(self):
        self.db.executescript('''
            CREATE TRIGGER reject_move BEFORE UPDATE ON current_revisions
            WHEN NEW.zone = 'west'
            BEGIN SELECT RAISE(ABORT, 'revision write rejected'); END;
        ''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'revision write rejected'):
            transfer(self.db, DAY, self.original, 'west', 7)
        self.assertEqual(read_current_revision(self.db, DAY, 'station', 'r1'), self.original)
        self.assertEqual([entry.seq for entry in read_postings(self.db, DAY, 0, 10)], [1])
        self.db.execute('DROP TRIGGER reject_move')
        self.assertEqual(transfer(self.db, DAY, self.original, 'west', 7).seq, 2)
