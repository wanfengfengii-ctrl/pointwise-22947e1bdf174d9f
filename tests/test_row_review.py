from contextlib import closing
from functools import partial
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from intake import BatchRequest, ReadingInput, submit
from inventory import freeze, read_inventory
from issue import issue_pack
from issue.codec import content_identity, decode_envelope, encode_envelope
from journal import correct_litres, initialize_source, read_current_revision, transfer
from restore import (
    initialize_isolated, read_received_package, read_row_review, receive_pack, review_rows,
)

DAY = '2026-04-12'
BATCH = 'station'

FREEZE = partial(freeze, row_evidence=True)
ISSUE = partial(issue_pack, row_evidence=True)


def trim(source, through):
    with source:
        source.execute('DELETE FROM postings WHERE day = ? AND seq <= ?', (DAY, through))


def build(source, exchange=True):
    initialize_source(source)
    initial = submit(source, BatchRequest(DAY, BATCH, (
        ReadingInput('a', 'east', 5), ReadingInput('b', 'west', 5),
    )))
    FREEZE(source, DAY, 'east', 'east-start', initial.posting_seq)
    FREEZE(source, DAY, 'west', 'west-start', initial.posting_seq)
    first = read_current_revision(source, DAY, BATCH, 'a')
    middle = (transfer(source, DAY, first, 'west', 5) if exchange
              else correct_litres(source, DAY, first, 5))
    FREEZE(source, DAY, 'west', 'west-middle', middle.seq)
    FREEZE(source, DAY, 'west', 'west-spare', middle.seq)
    second = read_current_revision(source, DAY, BATCH, 'b')
    last = (transfer(source, DAY, second, 'east', 5) if exchange
            else correct_litres(source, DAY, second, 5))
    FREEZE(source, DAY, 'east', 'east-end', last.seq)
    FREEZE(source, DAY, 'west', 'west-end', last.seq)
    trim(source, initial.posting_seq)
    staggered = ISSUE(source, DAY, 'staggered', ('east-start', 'west-middle'), last.seq)
    alternate = ISSUE(source, DAY, 'alternate', ('east-start', 'west-spare'), last.seq)
    before = ISSUE(source, DAY, 'before', ('east-start', 'west-start'), initial.posting_seq)
    trim(source, last.seq)
    after = ISSUE(source, DAY, 'after', ('east-end', 'west-end'), last.seq)
    return {'before': before, 'after': after, 'staggered': staggered,
            'alternate': alternate}


def receive_all(db, deliveries):
    for name, package in deliveries.items():
        receive_pack(db, name, package.raw)


def rebind_identity_only(body, issue_id):
    body['manifest']['issue_id'] = issue_id
    counts = {entry['zone']: entry for entry in body['counts']}
    evidence = {entry['zone']: entry['rows'] for entry in body['row_evidence']}
    for reference in body['manifest']['inventory']:
        zone = reference['zone']
        reference['identity'] = content_identity(
            {'count': counts[zone], 'evidence': evidence[zone]})


def rebind(body, issue_id):
    """Recompute manifest references after editing count payloads and evidence."""
    body['manifest']['issue_id'] = issue_id
    counts = {entry['zone']: entry for entry in body['counts']}
    evidence = {entry['zone']: entry['rows'] for entry in body['row_evidence']}
    for reference in body['manifest']['inventory']:
        zone = reference['zone']
        reference['identity'] = content_identity(
            {'count': counts[zone], 'evidence': evidence[zone]})


class RowReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        source_path = self.directory / 'source.sqlite'
        with closing(sqlite3.connect(source_path)) as source:
            self.deliveries = build(source)
        source_path.unlink()
        self.isolated_path = self.directory / 'isolated.sqlite'
        self.db = self.enterContext(closing(sqlite3.connect(self.isolated_path)))
        initialize_isolated(self.db)
        receive_all(self.db, self.deliveries)

    def rows(self, review):
        return [(row['batch_id'], row['line_id'], row['before'], row['after'], row['changes'])
                for row in review['rows']]

    def test_exchange_reports_both_rows_moved(self):
        review = review_rows(self.db, 'before', 'after', 'close')
        self.assertEqual((review['review_id'], review['day']), ('close', DAY))
        self.assertEqual(review['before'], {
            'receive_id': 'before', 'issue_id': 'before',
            'identity': self.deliveries['before'].identity, 'target': 1})
        self.assertEqual((review['after']['receive_id'], review['after']['target']),
                         ('after', 3))
        self.assertEqual(self.rows(review), [
            (BATCH, 'a',
             {'zone': 'east', 'litres': 5, 'revision': 1, 'source': {'count_id': 'east-start'}},
             {'zone': 'west', 'litres': 5, 'revision': 2, 'source': {'count_id': 'west-end'}},
             ['moved']),
            (BATCH, 'b',
             {'zone': 'west', 'litres': 5, 'revision': 1, 'source': {'count_id': 'west-start'}},
             {'zone': 'east', 'litres': 5, 'revision': 2, 'source': {'count_id': 'east-end'}},
             ['moved']),
        ])
        for name in ('before', 'after'):
            body = decode_envelope(self.deliveries[name].raw).body
            totals = {entry['zone']: (entry['rows'], entry['litres'])
                      for entry in body['counts']}
            self.assertEqual(totals, {'east': (1, 5), 'west': (1, 5)})

    def test_equal_value_correction_reports_same(self):
        with closing(sqlite3.connect(':memory:')) as source:
            deliveries = build(source, exchange=False)
        with closing(sqlite3.connect(':memory:')) as isolated:
            initialize_isolated(isolated)
            receive_all(isolated, deliveries)
            review = review_rows(isolated, 'before', 'after', 'close')
            self.assertEqual(self.rows(review), [
                (BATCH, 'a',
                 {'zone': 'east', 'litres': 5, 'revision': 1, 'source': {'count_id': 'east-start'}},
                 {'zone': 'east', 'litres': 5, 'revision': 2, 'source': {'count_id': 'east-end'}},
                 ['same']),
                (BATCH, 'b',
                 {'zone': 'west', 'litres': 5, 'revision': 1, 'source': {'count_id': 'west-start'}},
                 {'zone': 'west', 'litres': 5, 'revision': 2, 'source': {'count_id': 'west-end'}},
                 ['same']),
            ])

    def test_staggered_and_alternate_evidence(self):
        staggered = review_rows(self.db, 'before', 'staggered', 'staggered-review')
        self.assertEqual(self.rows(staggered), [
            (BATCH, 'a',
             {'zone': 'east', 'litres': 5, 'revision': 1, 'source': {'count_id': 'east-start'}},
             {'zone': 'west', 'litres': 5, 'revision': 2, 'source': {'count_id': 'west-middle'}},
             ['moved']),
            (BATCH, 'b',
             {'zone': 'west', 'litres': 5, 'revision': 1, 'source': {'count_id': 'west-start'}},
             {'zone': 'east', 'litres': 5, 'revision': 2, 'source': {'seq': 3}},
             ['moved']),
        ])
        alternate = review_rows(self.db, 'before', 'alternate', 'alternate-review')
        self.assertEqual(alternate['rows'][0]['after']['source'], {'count_id': 'west-spare'})

    def test_reopen_and_replay(self):
        first = review_rows(self.db, 'before', 'after', 'close')
        arrival = read_received_package(self.db, 'before')
        self.db.close()
        with closing(sqlite3.connect(self.isolated_path)) as reopened:
            initialize_isolated(reopened)
            self.assertEqual(read_row_review(reopened, 'close'), first)
            self.assertEqual(review_rows(reopened, 'before', 'after', 'close'), first)
            with self.assertRaisesRegex(ValueError, 'review id already binds'):
                review_rows(reopened, 'before', 'staggered', 'close')
            self.assertEqual(read_received_package(reopened, 'before'), arrival)
            self.assertIsNone(read_row_review(reopened, 'unknown'))

    def test_v1_delivery_is_rejected(self):
        with closing(sqlite3.connect(':memory:')) as source:
            initialize_source(source)
            initial = submit(source, BatchRequest(DAY, BATCH, (
                ReadingInput('a', 'east', 5), ReadingInput('b', 'west', 5),
            )))
            freeze(source, DAY, 'east', 'east-old', initial.posting_seq)
            freeze(source, DAY, 'west', 'west-old', initial.posting_seq)
            first = issue_pack(source, DAY, 'old', ('east-old', 'west-old'),
                               initial.posting_seq)
            transfer(source, DAY, read_current_revision(source, DAY, BATCH, 'b'), 'east', 5)
            freeze(source, DAY, 'east', 'east-new', 2)
            freeze(source, DAY, 'west', 'west-new', 2)
            second = issue_pack(source, DAY, 'new', ('east-new', 'west-new'), 2)
        with closing(sqlite3.connect(':memory:')) as isolated:
            initialize_isolated(isolated)
            receive_pack(isolated, 'old', first.raw)
            receive_pack(isolated, 'new', second.raw)
            with self.assertRaisesRegex(ValueError, 'no row evidence'):
                review_rows(isolated, 'old', 'new', 'v1')
            self.assertIsNone(read_row_review(isolated, 'v1'))
            self.assertEqual(isolated.execute('SELECT count(*) FROM row_reviews').fetchone(),
                             (0,))

    def test_tampered_evidence_rejected_without_result(self):
        body = decode_envelope(self.deliveries['after'].raw).body
        body['manifest']['issue_id'] = 'tampered'
        body['row_evidence'][0]['rows'][0][4] = 6
        receive_pack(self.db, 'tampered', encode_envelope(body))
        with self.assertRaisesRegex(ValueError, 'block disagrees|does not reproduce|identity'):
            review_rows(self.db, 'before', 'tampered', 'bad')
        self.assertIsNone(read_row_review(self.db, 'bad'))

    def test_conflicting_evidence_rejected(self):
        # The later endpoint claims row b is still in the west at revision 1
        # while its own continuation moves b to the east at revision 2.
        body = decode_envelope(self.deliveries['after'].raw).body
        for entry in body['counts']:
            if entry['zone'] == 'west':
                entry['rows'] += 1
                entry['litres'] += 5
        for entry in body['row_evidence']:
            if entry['zone'] == 'west':
                entry['rows'].append(['station', 'b', 1, 'west', 5])
        rebind(body, 'conflict')
        receive_pack(self.db, 'conflict', encode_envelope(body))
        with self.assertRaisesRegex(ValueError, 'revision went backwards|contradicts|conflict'):
            review_rows(self.db, 'before', 'conflict', 'bad')
        self.assertIsNone(read_row_review(self.db, 'bad'))

    def test_missing_later_row_rejected(self):
        body = decode_envelope(self.deliveries['after'].raw).body
        # Drop row b from the later evidence and its count so only the earlier
        # delivery still knows the row.
        for entry in body['counts']:
            if entry['zone'] == 'east':
                entry['rows'] = 0
        for entry in body['row_evidence']:
            if entry['zone'] == 'east':
                entry['rows'] = [row for row in entry['rows'] if row[1] != 'b']
        rebind(body, 'dropped')
        receive_pack(self.db, 'dropped', encode_envelope(body))
        with self.assertRaises(ValueError):
            review_rows(self.db, 'before', 'dropped', 'bad')
        self.assertIsNone(read_row_review(self.db, 'bad'))

    def test_foreign_source_rejected(self):
        # Point row b's provenance at a count the delivery does not select.
        body = decode_envelope(self.deliveries['after'].raw).body
        for reference in body['manifest']['inventory']:
            if reference['zone'] == 'east':
                reference['count_id'] = 'east-start'
        rebind_identity_only(body, 'foreign')
        receive_pack(self.db, 'foreign', encode_envelope(body))
        with self.assertRaises(ValueError):
            review_rows(self.db, 'before', 'foreign', 'bad')
        self.assertIsNone(read_row_review(self.db, 'bad'))

    def test_gap_and_added_row(self):
        with closing(sqlite3.connect(':memory:')) as source:
            initialize_source(source)
            initial = submit(source, BatchRequest(DAY, BATCH, (
                ReadingInput('a', 'east', 5), ReadingInput('b', 'west', 5),
            )))
            freeze(source, DAY, 'east', 'east-start', 1, row_evidence=True)
            freeze(source, DAY, 'west', 'west-start', 1, row_evidence=True)
            first = issue_pack(source, DAY, 'before', ('east-start', 'west-start'), 1,
                               row_evidence=True)
            extra = submit(source, BatchRequest(DAY, 'extra', (ReadingInput('c', 'east', 3),)))
            freeze(source, DAY, 'east', 'east-late', extra.posting_seq, row_evidence=True)
            freeze(source, DAY, 'west', 'west-late', extra.posting_seq, row_evidence=True)
            later = issue_pack(source, DAY, 'after', ('east-late', 'west-late'),
                               extra.posting_seq, row_evidence=True)
        with closing(sqlite3.connect(':memory:')) as isolated:
            initialize_isolated(isolated)
            receive_pack(isolated, 'before', first.raw)
            receive_pack(isolated, 'after', later.raw)
            review = review_rows(isolated, 'before', 'after', 'gap')
            self.assertEqual(review['rows'], [
                {'batch_id': 'extra', 'line_id': 'c', 'before': None,
                 'after': {'zone': 'east', 'litres': 3, 'revision': 1,
                           'source': {'count_id': 'east-late'}},
                 'changes': ['added']},
                {'batch_id': BATCH, 'line_id': 'a',
                 'before': {'zone': 'east', 'litres': 5, 'revision': 1,
                            'source': {'count_id': 'east-start'}},
                 'after': {'zone': 'east', 'litres': 5, 'revision': 1,
                           'source': {'count_id': 'east-late'}},
                 'changes': ['same']},
                {'batch_id': BATCH, 'line_id': 'b',
                 'before': {'zone': 'west', 'litres': 5, 'revision': 1,
                            'source': {'count_id': 'west-start'}},
                 'after': {'zone': 'west', 'litres': 5, 'revision': 1,
                           'source': {'count_id': 'west-late'}},
                 'changes': ['same']},
            ])

    def test_moved_and_changed_together(self):
        with closing(sqlite3.connect(':memory:')) as source:
            initialize_source(source)
            initial = submit(source, BatchRequest(DAY, BATCH, (
                ReadingInput('a', 'east', 5), ReadingInput('b', 'west', 5),
            )))
            freeze(source, DAY, 'east', 'east-start', 1, row_evidence=True)
            freeze(source, DAY, 'west', 'west-start', 1, row_evidence=True)
            before = issue_pack(source, DAY, 'before', ('east-start', 'west-start'), 1,
                                row_evidence=True)
            moved = transfer(source, DAY, read_current_revision(source, DAY, BATCH, 'a'),
                             'west', 7)
            freeze(source, DAY, 'east', 'east-end', moved.seq, row_evidence=True)
            freeze(source, DAY, 'west', 'west-end', moved.seq, row_evidence=True)
            after = issue_pack(source, DAY, 'after', ('east-end', 'west-end'), moved.seq,
                               row_evidence=True)
        with closing(sqlite3.connect(':memory:')) as isolated:
            initialize_isolated(isolated)
            receive_pack(isolated, 'before', before.raw)
            receive_pack(isolated, 'after', after.raw)
            review = review_rows(isolated, 'before', 'after', 'mixed')
            a = review['rows'][0]
            self.assertEqual(a['changes'], ['moved', 'changed'])
            self.assertEqual((a['after']['zone'], a['after']['litres']), ('west', 7))
            self.assertEqual(review['rows'][1]['changes'], ['same'])

    def test_ordering_rejected(self):
        for before, after, review_id in (('after', 'before', 'backwards'),
                                         ('after', 'after', 'equal')):
            with self.subTest(review_id=review_id), \
                    self.assertRaisesRegex(ValueError, 'must precede'):
                review_rows(self.db, before, after, review_id)
            self.assertIsNone(read_row_review(self.db, review_id))

    def test_shared_window_conflict_rejected(self):
        with closing(sqlite3.connect(':memory:')) as source:
            initialize_source(source)
            initial = submit(source, BatchRequest(DAY, BATCH, (
                ReadingInput('a', 'east', 5), ReadingInput('b', 'west', 5),
            )))
            freeze(source, DAY, 'east', 'east-start', 1, row_evidence=True)
            freeze(source, DAY, 'west', 'west-start', 1, row_evidence=True)
            before = issue_pack(source, DAY, 'before', ('east-start', 'west-start'), 1,
                                row_evidence=True)
            transfer(source, DAY, read_current_revision(source, DAY, BATCH, 'a'), 'west', 5)
            freeze(source, DAY, 'east', 'east-mid', 2, row_evidence=True)
            freeze(source, DAY, 'west', 'west-mid', 2, row_evidence=True)
            mid = issue_pack(source, DAY, 'mid', ('east-mid', 'west-mid'), 2,
                             row_evidence=True)
            correct_litres(source, DAY, read_current_revision(source, DAY, BATCH, 'b'), 6)
            freeze(source, DAY, 'east', 'east-end', 3, row_evidence=True)
            freeze(source, DAY, 'west', 'west-end', 3, row_evidence=True)
            genuine = issue_pack(source, DAY, 'after', ('east-start', 'west-end'), 3,
                                 row_evidence=True)
        body = decode_envelope(genuine.raw).body
        # Rewrite the shared posting as a transfer at 7 L with a following
        # correction back to 5 L: internally consistent at target 3, but it
        # contradicts the earlier delivery over the shared window.
        body['postings'][0] = {'day': DAY, 'seq': 2, 'kind': 'transfer',
                               'batch_id': BATCH,
                               'lines': [['a', 2]],
                               'effects': [{'zone': 'east', 'rows': -1, 'litres': -5},
                                           {'zone': 'west', 'rows': 1, 'litres': 7}]}
        body['postings'][1] = {'day': DAY, 'seq': 3, 'kind': 'correction',
                               'batch_id': BATCH,
                               'lines': [['a', 3]],
                               'effects': [{'zone': 'west', 'rows': 0, 'litres': -2}]}
        for entry in body['row_evidence']:
            for row in entry['rows']:
                if row[1] == 'a' and row[3] == 'west':
                    row[2] = 3
        body['manifest']['issue_id'] = 'rewritten'
        counts = {entry['zone']: entry for entry in body['counts']}
        for reference in body['manifest']['inventory']:
            reference['identity'] = content_identity(
                {'count': counts[reference['zone']],
                 'evidence': body['row_evidence'][
                     [item['zone'] for item in body['row_evidence']].index(reference['zone'])
                 ]['rows']})
        with closing(sqlite3.connect(':memory:')) as isolated:
            initialize_isolated(isolated)
            receive_pack(isolated, 'before', before.raw)
            receive_pack(isolated, 'mid', mid.raw)
            receive_pack(isolated, 'rewritten', encode_envelope(body))
            with self.assertRaisesRegex(ValueError, 'shared window|earlier target account'):
                review_rows(isolated, 'mid', 'rewritten', 'bad')
            self.assertIsNone(read_row_review(isolated, 'bad'))

    def test_cross_day_rejected(self):
        other_day = '2026-04-13'
        with closing(sqlite3.connect(':memory:')) as source:
            initialize_source(source)
            submit(source, BatchRequest(other_day, BATCH, (
                ReadingInput('a', 'east', 5), ReadingInput('b', 'west', 5))))
            freeze(source, other_day, 'east', 'east-start', 1, row_evidence=True)
            freeze(source, other_day, 'west', 'west-start', 1, row_evidence=True)
            other_before = issue_pack(source, other_day, 'other-before',
                                      ('east-start', 'west-start'), 1, row_evidence=True)
            transfer(source, other_day,
                     read_current_revision(source, other_day, BATCH, 'a'), 'west', 5)
            freeze(source, other_day, 'east', 'east-end', 2, row_evidence=True)
            freeze(source, other_day, 'west', 'west-end', 2, row_evidence=True)
            other_after = issue_pack(source, other_day, 'other-after',
                                     ('east-end', 'west-end'), 2, row_evidence=True)
        receive_pack(self.db, 'other-before', other_before.raw)
        receive_pack(self.db, 'other-after', other_after.raw)
        for before, after, review_id in (('before', 'other-after', 'mixed-day'),
                                         ('other-before', 'after', 'mixed-day-2')):
            with self.subTest(review_id=review_id), \
                    self.assertRaisesRegex(ValueError, 'same day'):
                review_rows(self.db, before, after, review_id)
            self.assertIsNone(read_row_review(self.db, review_id))

    def test_unknown_receive_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unknown received delivery'):
            review_rows(self.db, 'before', 'absent', 'bad')
        self.assertIsNone(read_row_review(self.db, 'bad'))

    def test_evidence_requires_both_frozen_counts(self):
        with closing(sqlite3.connect(':memory:')) as source:
            initialize_source(source)
            initial = submit(source, BatchRequest(DAY, BATCH, (
                ReadingInput('a', 'east', 5), ReadingInput('b', 'west', 5),
            )))
            freeze(source, DAY, 'east', 'east-v1', initial.posting_seq)
            freeze(source, DAY, 'west', 'west-v2', initial.posting_seq, row_evidence=True)
            for ids in (('east-v1', 'west-v2'), ('west-v2', 'east-v1')):
                with self.subTest(ids=ids), \
                        self.assertRaisesRegex(ValueError, 'lacks frozen row evidence'):
                    issue_pack(source, DAY, 'mix', ids, initial.posting_seq,
                               row_evidence=True)
            self.assertIsNone(source.execute(
                "SELECT 1 FROM issued_packages WHERE issue_id = 'mix'").fetchone())

    def test_v2_evidence_survives_retention(self):
        with closing(sqlite3.connect(':memory:')) as source:
            initialize_source(source)
            initial = submit(source, BatchRequest(DAY, BATCH, (
                ReadingInput('a', 'east', 5), ReadingInput('b', 'west', 5),
            )))
            freeze(source, DAY, 'east', 'east-start', initial.posting_seq, row_evidence=True)
            freeze(source, DAY, 'west', 'west-start', initial.posting_seq, row_evidence=True)
            original = read_inventory(source, 'east-start')
            trim(source, initial.posting_seq)
            self.assertEqual(read_inventory(source, 'east-start'), original)
            package = issue_pack(source, DAY, 'empty', ('east-start', 'west-start'), 1,
                                 row_evidence=True)
            body = decode_envelope(package.raw).body
            self.assertEqual(body['postings'], [])
            self.assertEqual(body['row_evidence'][0]['rows'],
                             [['station', 'a', 1, 'east', 5]])

    def test_initialize_source_upgrades_old_inventories(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        path = directory / 'legacy.sqlite'
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''
                CREATE TABLE inventories (
                    count_id TEXT PRIMARY KEY, day TEXT NOT NULL, zone TEXT NOT NULL,
                    cut INTEGER NOT NULL, rows INTEGER NOT NULL, litres TEXT NOT NULL,
                    origin TEXT NOT NULL);
            ''')
            db.execute("INSERT INTO inventories VALUES "
                       "('old', '2026-04-12', 'east', 0, 0, '0', 'inventory/old')")
            db.commit()
            initialize_source(db)
            initialize_source(db)
            columns = {row[1] for row in db.execute('PRAGMA table_info(inventories)')}
            self.assertIn('evidence_json', columns)
            self.assertEqual(
                db.execute("SELECT count_id, evidence_json FROM inventories").fetchall(),
                [('old', None)])

    def test_v2_freeze_rejects_lost_history(self):
        with closing(sqlite3.connect(':memory:')) as source:
            initialize_source(source)
            initial = submit(source, BatchRequest(DAY, BATCH, (
                ReadingInput('a', 'east', 5),)),
            )
            with source:
                source.execute('DELETE FROM postings WHERE day = ? AND seq = 1', (DAY,))
            with self.assertRaisesRegex(ValueError, 'incomplete continuation'):
                freeze(source, DAY, 'east', 'east-lost', initial.posting_seq,
                       row_evidence=True)
            self.assertIsNone(read_inventory(source, 'east-lost'))


if __name__ == '__main__':
    unittest.main()
