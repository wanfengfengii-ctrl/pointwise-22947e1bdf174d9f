from contextlib import closing
import sqlite3
import unittest

from intake import BatchRequest, ReadingInput, submit
from inventory import freeze, read_inventory
from issue import issue_pack, read_issued_package
from issue.codec import decode_envelope
from journal import balance_at_cut, correct_litres, initialize_source, read_current_revision

DAY = '2026-04-12'
OTHER_DAY = '2026-04-13'


class IssueTests(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(closing(sqlite3.connect(':memory:')))
        initialize_source(self.db)
        submit(self.db, BatchRequest(DAY, 'station', (ReadingInput('r1', 'east', 5),)))
        self.east = freeze(self.db, DAY, 'east', 'east-old', 1)
        self.west = freeze(self.db, DAY, 'west', 'west-old', 1)

    def correct(self, litres):
        return correct_litres(self.db, DAY, read_current_revision(self.db, DAY, 'station', 'r1'), litres)

    def test_t3_original_snapshot_and_same_zone_correction(self):
        posting = self.correct(8)
        self.assertEqual((posting.seq, posting.kind, posting.lines), (2, 'correction', (('r1', 2),)))
        self.assertEqual([(effect.zone, effect.rows, effect.litres) for effect in posting.effects],
                         [('east', 0, 3)])
        old = read_inventory(self.db, 'east-old')
        self.assertEqual(old, self.east)
        self.assertEqual((old.day, old.zone, old.cut, old.rows, old.litres, old.origin),
                         (DAY, 'east', 1, 1, 5, f'inventory/{DAY}/east/east-old'))
        self.assertEqual((self.west.rows, self.west.litres), (0, 0))
        new = balance_at_cut(self.db, DAY, 'east', 2)
        self.assertEqual((new.rows, new.litres), (1, 8))
        self.assertEqual(freeze(self.db, DAY, 'east', 'retro', 1).litres, 5)
        with self.assertRaisesRegex(ValueError, 'unknown formal cut'):
            freeze(self.db, DAY, 'west', 'future', 3)
        self.assertIsNone(read_inventory(self.db, 'future'))

    def test_t3_issued_originals_and_reissue_original(self):
        self.correct(8)
        late = freeze(self.db, DAY, 'east', 'east-late', 2)
        package = issue_pack(self.db, DAY, 'shift', ('west-old', 'east-late'), 2)
        envelope = decode_envelope(package.raw)
        self.assertEqual(envelope.identity, package.identity)
        manifest = envelope.body['manifest']
        self.assertEqual((manifest['day'], manifest['issue_id'], manifest['start'], manifest['target']),
                         (DAY, 'shift', 1, 2))
        self.assertEqual([(ref['zone'], ref['count_id'], ref['cut'], ref['origin'])
                          for ref in manifest['inventory']],
                         [('east', 'east-late', 2, late.origin), ('west', 'west-old', 1, self.west.origin)])
        self.assertEqual([(item['count_id'], item['rows'], item['litres'])
                          for item in envelope.body['counts']],
                         [('east-late', 1, 8), ('west-old', 0, 0)])
        self.assertEqual([(p['seq'], p['effects']) for p in envelope.body['postings']],
                         [(2, [{'zone': 'east', 'rows': 0, 'litres': 3}])])
        self.correct(12)
        initialize_source(self.db)
        self.assertEqual(read_issued_package(self.db, 'shift'), package)
        self.assertEqual(read_inventory(self.db, 'east-late'), late)

    def test_t4_target_between_cuts(self):
        self.correct(6)
        self.correct(8)
        freeze(self.db, DAY, 'west', 'west-late', 3)
        with self.assertRaisesRegex(ValueError, 'target precedes'):
            issue_pack(self.db, DAY, 'shift', ('east-old', 'west-late'), 2)
        self.assertIsNone(read_issued_package(self.db, 'shift'))
        with self.assertRaisesRegex(ValueError, 'unknown formal cut'):
            issue_pack(self.db, DAY, 'shift', ('east-old', 'west-late'), 4)
        self.assertIsNone(read_issued_package(self.db, 'shift'))
        self.assertEqual(issue_pack(self.db, DAY, 'shift', ('east-old', 'west-late'), 3).target, 3)

    def test_t4_missing_or_cross_day(self):
        submit(self.db, BatchRequest(OTHER_DAY, 'station', (ReadingInput('r1', 'west', 5),)))
        freeze(self.db, OTHER_DAY, 'west', 'other-west', 1)
        for ids in (('east-old',), ('east-old', 'absent'), ('east-old', 'other-west'),
                    ('east-old', 'east-old')):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                issue_pack(self.db, DAY, 'shift', ids, 1)
        self.assertIsNone(read_issued_package(self.db, 'shift'))

    def test_t4_empty_window_and_missing_suffix(self):
        empty = issue_pack(self.db, DAY, 'empty', ('east-old', 'west-old'), 1)
        self.assertEqual((empty.start, empty.target), (1, 1))
        self.assertEqual(decode_envelope(empty.raw).body['postings'], [])
        self.correct(6)
        self.correct(8)
        with self.db:
            self.db.execute('DELETE FROM postings WHERE day = ? AND seq = 2', (DAY,))
        with self.assertRaisesRegex(ValueError, 'incomplete continuation'):
            issue_pack(self.db, DAY, 'gap', ('east-old', 'west-old'), 3)
        self.assertIsNone(read_issued_package(self.db, 'gap'))
