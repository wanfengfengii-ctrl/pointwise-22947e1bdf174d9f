"""Per-row review of two staggered same-day V2 handoffs.

Each handoff is checked only against the material it itself commits: the
identified partition originals, their frozen row evidence, and the formal
continuation window.  The review then compares the two endpoint row accounts.
"""
from dataclasses import dataclass
import json
import sqlite3

from issue.codec import content_identity, decode_envelope, require_fields
from issue.effects import parse_window
from issue.manifests import parse_manifest, parse_row_evidence
from journal.balances import ZONES
from journal.postings import Posting, require_label

from .acceptance import ReceivedPackage, read_received_package
from .store import isolated_transaction

_COUNT_FIELDS = {'day', 'zone', 'count_id', 'rows', 'litres', 'origin'}


@dataclass(frozen=True, slots=True)
class ParsedEndpoint:
    receive_id: str
    day: str
    issue_id: str
    identity: str
    start: int
    target: int
    counts: dict          # zone -> (count payload, evidence rows, manifest reference)
    postings: tuple[Posting, ...]


def _evidence_entry(entry: object) -> list:
    if (type(entry) is not list or len(entry) != 5
            or type(entry[2]) is not int or type(entry[4]) is not int):
        raise ValueError('invalid row evidence entry')
    batch_id, line_id, revision, zone, litres = entry
    require_label(batch_id, 'batch id')
    require_label(line_id, 'line id')
    if revision < 1 or litres < 0:
        raise ValueError('invalid row evidence state')
    return [batch_id, line_id, revision, zone, litres]


def _parse_endpoint(received: ReceivedPackage) -> ParsedEndpoint:
    envelope = decode_envelope(received.raw)
    if envelope.identity != received.identity:
        raise ValueError('received version identity mismatch')
    fields = {'manifest', 'counts', 'postings'}
    if type(envelope.body) is dict and envelope.body.keys() == fields | {'row_evidence'}:
        fields = fields | {'row_evidence'}
    body = require_fields(envelope.body, fields, 'handoff body')
    manifest = parse_manifest(body['manifest'])
    if not manifest.row_evidence or 'row_evidence' not in body:
        raise ValueError('delivery carries no row evidence')
    if (manifest.day, manifest.issue_id) != (received.day, received.issue_id):
        raise ValueError('received delivery does not match its manifest')

    block = parse_row_evidence(body['row_evidence'], manifest)

    raw_counts = body['counts']
    if type(raw_counts) is not list or len(raw_counts) != len(manifest.inventory):
        raise ValueError('received inventories are incomplete')
    expected = {reference.count_id: reference for reference in manifest.inventory}
    counts = {}
    for item in raw_counts:
        count = require_fields(item, _COUNT_FIELDS, 'received inventory')
        count_id = count['count_id']
        require_label(count_id, 'count id')
        reference = expected.pop(count_id, None)
        if reference is None:
            raise ValueError(f'unselected or repeated inventory {count_id}')
        if (count['day'] != manifest.day or count['zone'] != reference.zone
                or count['origin'] != reference.origin):
            raise ValueError(f'received inventory does not match reference {count_id}')
        if (type(count['rows']) is not int or type(count['litres']) is not int
                or count['rows'] < 0 or count['litres'] < 0):
            raise ValueError('inconsistent inventory measurement')
        rows = block[reference.zone]
        if content_identity({'count': count, 'evidence': rows}) != reference.identity:
            raise ValueError(f'inventory identity mismatch for {count_id}')
        if (len(rows), sum(entry[4] for entry in rows)) != (count['rows'], count['litres']):
            raise ValueError(f'row evidence does not reproduce {count_id}')
        counts[reference.zone] = (count, tuple(tuple(entry) for entry in rows), reference)
    if expected or set(counts) != set(ZONES):
        raise ValueError('received inventories are incomplete')

    postings = parse_window(body['postings'], manifest.day, manifest.start, manifest.target)
    return ParsedEndpoint(received.receive_id, manifest.day, manifest.issue_id,
                          received.identity, manifest.start, manifest.target, counts, postings)


# --- row replay ------------------------------------------------------------

def _split_transfer(posting: Posting):
    outgoing = [effect for effect in posting.effects if effect.rows == -1]
    incoming = [effect for effect in posting.effects if effect.rows == 1]
    if len(outgoing) != 1 or len(incoming) != 1 or outgoing[0].zone == incoming[0].zone:
        raise ValueError('invalid transfer zone effects')
    return outgoing[0], incoming[0]


State = tuple[int, str, int]


def _amended(state: State, posting: Posting) -> State:
    """Apply one amendment, checking its revision and its row-level effects."""
    revision, zone, litres = state
    if len(posting.lines) != 1:
        raise ValueError('amendment must reference one observation')
    if posting.lines[0][1] != revision + 1:
        raise ValueError('posting revision does not continue the row history')
    if posting.kind == 'transfer':
        outgoing, incoming = _split_transfer(posting)
        if outgoing.zone != zone or outgoing.litres != -litres:
            raise ValueError('transfer does not match the outgoing row')
        return posting.lines[0][1], incoming.zone, incoming.litres
    if posting.kind != 'correction':
        raise ValueError('unknown amendment kind')
    if len(posting.effects) != 1 or posting.effects[0].rows != 0:
        raise ValueError('invalid correction posting')
    effect = posting.effects[0]
    if effect.zone != zone:
        raise ValueError('correction does not match the row partition')
    updated = litres + effect.litres
    if updated < 0:
        raise ValueError('correction leaves a negative reading')
    return posting.lines[0][1], zone, updated


def _reverted(state: State, posting: Posting) -> State:
    """Undo one amendment to expose the state immediately before it."""
    revision, zone, litres = state
    if len(posting.lines) != 1 or posting.lines[0][1] != revision:
        raise ValueError('revision chain does not meet the later row evidence')
    if revision <= 1:
        raise ValueError('later row evidence implies an impossible revision')
    if posting.kind == 'transfer':
        outgoing, incoming = _split_transfer(posting)
        if incoming.zone != zone or incoming.litres != litres:
            raise ValueError('transfer does not lead to the evidenced row')
        return revision - 1, outgoing.zone, -outgoing.litres
    if posting.kind != 'correction':
        raise ValueError('unknown amendment kind')
    if len(posting.effects) != 1 or posting.effects[0].rows != 0:
        raise ValueError('invalid correction posting')
    effect = posting.effects[0]
    if effect.zone != zone:
        raise ValueError('correction does not match the row partition')
    return revision - 1, zone, litres - effect.litres


def _index_rows(endpoint: ParsedEndpoint):
    anchors, amendments, intakes = {}, {}, {}
    for zone in ZONES:
        _count, rows, reference = endpoint.counts[zone]
        for batch_id, line_id, revision, _zone, litres in rows:
            anchors.setdefault((batch_id, line_id), []).append(
                (reference.cut, revision, zone, litres, reference.count_id))
    for chain in anchors.values():
        chain.sort(key=lambda anchor: anchor[0])
        for earlier, later in zip(chain, chain[1:]):
            if later[1] < earlier[1]:
                raise ValueError('revision went backwards inside the row evidence')
            if later[1] == earlier[1] and (later[2], later[3]) != (earlier[2], earlier[3]):
                raise ValueError('same revision carries conflicting row states')
    for posting in endpoint.postings:
        for line_id, _revision in posting.lines:
            key = (posting.batch_id, line_id)
            target = intakes if posting.kind == 'intake' else amendments
            target.setdefault(key, []).append((posting.seq, posting))
    for chain in amendments.values():
        chain.sort(key=lambda pair: pair[0])
        revisions = [posting.lines[0][1] for _seq, posting in chain]
        for previous, following in zip(revisions, revisions[1:]):
            if following != previous + 1:
                raise ValueError('amendment revisions do not advance one at a time')
    return anchors, amendments, intakes


def _seed(amendments, intakes):
    """Reconstruct revision 1 of a row whose intake lies inside the window."""
    if not intakes:
        return None
    seq, posting = intakes[0]
    if len(posting.lines) != 1 or len(posting.effects) != 1 or posting.effects[0].rows != 1:
        raise ValueError('insufficient row evidence for an unpinned observation')
    effect = posting.effects[0]
    if effect.litres < 0:
        raise ValueError('invalid intake effect')
    if any(change_seq <= seq for change_seq, _change in amendments):
        raise ValueError('amendment precedes the row intake')
    return (1, effect.zone, effect.litres), seq


def _derive_unpinned(amendments, intakes, limit):
    """Derive a row that no selected count pins, e.g. an intake in the window."""
    seeded = _seed(amendments, intakes)
    if seeded is None:
        raise ValueError('continuation references an observation without row evidence')
    state, seq = seeded
    if limit < seq:
        return None
    establishing = seq
    for change_seq, change in amendments:
        if change_seq > limit:
            break
        state = _amended(state, change)
        establishing = change_seq
    return state, establishing


def _derive(anchors, amendments, intakes, limit):
    """Derive one row's state at formal ``limit`` from a single delivery.

    Returns ``(state, establishing_seq)`` or ``None`` when the row does not
    exist yet.  ``establishing_seq`` is ``None`` when frozen evidence pins the
    state directly.
    """
    chain = anchors
    changes = amendments
    if not chain:
        return _derive_unpinned(changes, intakes, limit)
    base_cut, base_revision, base_zone, base_litres, _count_id = chain[0]
    seeded = _seed(changes, intakes)
    if seeded is not None:
        # An intake inside the window lets us replay the row from revision 1
        # and prove the counted state, not merely match revision numbers.
        state, _seq = seeded
        for seq, posting in changes:
            if seq > base_cut:
                break
            state = _amended(state, posting)
        if state != (base_revision, base_zone, base_litres):
            raise ValueError('continuation contradicts the frozen row evidence')
    else:
        previous = 0
        reached = False
        for seq, posting in changes:
            if seq > base_cut:
                break
            revision = posting.lines[0][1]
            if revision <= previous or revision > base_revision:
                raise ValueError('continuation contradicts the frozen row evidence')
            previous = revision
            reached = True
        if reached and previous != base_revision:
            raise ValueError('continuation does not reach the counted revision')

    if base_cut > limit:
        # The row is first pinned after the requested point: undo the formal
        # changes in between to expose the earlier state.  An intake inside the
        # interval means the row did not exist yet at ``limit``.
        for seq, _posting in intakes:
            if limit < seq <= base_cut:
                return None
        state: State = (base_revision, base_zone, base_litres)
        for seq, posting in reversed(changes):
            if seq <= limit or seq > base_cut:
                continue
            state = _reverted(state, posting)
        establishing = None
        for seq, _posting in intakes:
            if seq <= limit:
                establishing = seq
        for seq, _posting in changes:
            if seq <= limit:
                establishing = seq
        return state, establishing

    state = (base_revision, base_zone, base_litres)
    establishing = None
    index = 1
    for seq, posting in changes:
        if seq <= base_cut:
            continue
        if seq > limit:
            break
        while index < len(chain) and chain[index][0] < seq:
            _cut, revision, zone, litres, _cid = chain[index]
            state = _agrees(state, (revision, zone, litres))
            index += 1
        if posting.kind == 'intake':
            raise ValueError('intake repeats an observation already evidenced')
        state = _amended(state, posting)
        establishing = seq
        while index < len(chain) and chain[index][0] == seq:
            _cut, revision, zone, litres, _cid = chain[index]
            state = _agrees(state, (revision, zone, litres))
            index += 1
    while index < len(chain) and chain[index][0] <= limit:
        _cut, revision, zone, litres, _cid = chain[index]
        state = _agrees(state, (revision, zone, litres))
        index += 1
    return state, establishing


def _agrees(state: State, expected: State) -> State:
    if state != expected:
        raise ValueError('row evidence conflicts with the continuation')
    return state


def _states(endpoint: ParsedEndpoint, limit: int) -> dict:
    anchors, amendments, intakes = _index_rows(endpoint)
    states = {}
    for key in set(anchors) | set(amendments) | set(intakes):
        derived = _derive(anchors.get(key, []), amendments.get(key, []),
                          intakes.get(key, []), limit)
        if derived is None:
            continue
        state, establishing = derived
        states[key] = {'revision': state[0], 'zone': state[1], 'litres': state[2],
                       'seq': establishing}
    return states


def _sourced(endpoint: ParsedEndpoint, limit: int) -> dict:
    """Row states at ``limit`` with a locatable provenance for each row."""
    states = _states(endpoint, limit)
    anchors, amendments, intakes = _index_rows(endpoint)
    for key in set(amendments) | set(intakes):
        if key not in states and any(seq <= limit for seq, _posting in amendments.get(key, ())):
            raise ValueError('continuation references an observation without row evidence')
    for key, state in states.items():
        _count, rows, reference = endpoint.counts[state['zone']]
        discovered = any(entry[0] == key[0] and entry[1] == key[1]
                         and entry[2] == state['revision'] and entry[4] == state['litres']
                         for entry in rows)
        if discovered:
            state['source'] = {'count_id': reference.count_id}
        elif state['seq'] is not None:
            state['source'] = {'seq': state['seq']}
        else:
            raise ValueError('cannot locate the observation provenance in this delivery')
    return states


def _partition_totals(endpoint: ParsedEndpoint) -> dict:
    totals = {}
    for zone in ZONES:
        count = endpoint.counts[zone][0]
        totals[zone] = [count['rows'], count['litres']]
    for posting in endpoint.postings:
        for effect in posting.effects:
            reference = endpoint.counts[effect.zone][2]
            if posting.seq > reference.cut:
                totals[effect.zone][0] += effect.rows
                totals[effect.zone][1] += effect.litres
    for zone in ZONES:
        rows, litres = totals[zone]
        if rows < 0 or litres < 0 or (rows == 0 and litres != 0):
            raise ValueError('continuation leaves an inconsistent partition')
    return totals


def _require_row_totals(endpoint: ParsedEndpoint, states: dict) -> None:
    totals = {zone: [0, 0] for zone in ZONES}
    for state in states.values():
        totals[state['zone']][0] += 1
        totals[state['zone']][1] += state['litres']
    if totals != _partition_totals(endpoint):
        raise ValueError('row states do not reproduce the restored partition totals')


# --- comparison ------------------------------------------------------------

def _changes(before: dict | None, after: dict | None) -> list[str]:
    if before is None:
        return ['added']
    changes = []
    if before['zone'] != after['zone']:
        changes.append('moved')
    if before['litres'] != after['litres']:
        changes.append('changed')
    return changes or ['same']


def _public_state(state: dict | None) -> dict | None:
    if state is None:
        return None
    return {'zone': state['zone'], 'litres': state['litres'], 'revision': state['revision'],
            'source': state['source']}


def _endpoint_header(received: ReceivedPackage, endpoint: ParsedEndpoint) -> dict:
    return {'receive_id': received.receive_id, 'issue_id': endpoint.issue_id,
            'identity': endpoint.identity, 'target': endpoint.target}


def _common_window(before: ParsedEndpoint, after: ParsedEndpoint) -> None:
    start = max(before.start, after.start)
    earlier = {posting.seq: posting for posting in before.postings
               if start < posting.seq <= before.target}
    later = {posting.seq: posting for posting in after.postings
             if start < posting.seq <= before.target}
    if earlier != later:
        raise ValueError('continuation postings disagree over the shared window')


def _prove_earlier_target(before_states: dict, after: ParsedEndpoint, target: int) -> None:
    derived = _states(after, target)
    if set(derived) != set(before_states):
        raise ValueError('deliveries disagree on the earlier target account')
    for key, earlier in before_states.items():
        view = derived[key]
        if (view['revision'], view['zone'], view['litres']) != (
                earlier['revision'], earlier['zone'], earlier['litres']):
            raise ValueError('deliveries disagree on the earlier target account')


def build_review(before_received: ReceivedPackage, after_received: ReceivedPackage,
                 review_id: str) -> dict:
    before = _parse_endpoint(before_received)
    after = _parse_endpoint(after_received)
    if before.day != after.day:
        raise ValueError('row review requires deliveries from the same day')
    if not before.target < after.target:
        raise ValueError('the earlier delivery target must precede the later target')

    before_states = _sourced(before, before.target)
    after_states = _sourced(after, after.target)
    _require_row_totals(before, before_states)
    _require_row_totals(after, after_states)

    if after.start <= before.target:
        _common_window(before, after)
        _prove_earlier_target(before_states, after, before.target)

    for key, earlier in before_states.items():
        later = after_states.get(key)
        if later is None:
            raise ValueError('an observation of the earlier delivery is missing later')
        if later['revision'] < earlier['revision']:
            raise ValueError('revision went backwards between the deliveries')
        if later['revision'] == earlier['revision'] and (
                later['zone'], later['litres']) != (earlier['zone'], earlier['litres']):
            raise ValueError('equal revisions carry conflicting row states')

    rows = []
    for key in sorted(set(before_states) | set(after_states)):
        earlier = before_states.get(key)
        later = after_states.get(key)
        rows.append({
            'batch_id': key[0], 'line_id': key[1],
            'before': _public_state(earlier), 'after': _public_state(later),
            'changes': _changes(earlier, later),
        })
    return {'review_id': review_id, 'day': before.day,
            'before': _endpoint_header(before_received, before),
            'after': _endpoint_header(after_received, after),
            'rows': rows}


# --- persistence -----------------------------------------------------------

def _save_review(db: sqlite3.Connection, result: dict) -> None:
    db.execute(
        '''INSERT INTO row_reviews
           (review_id, day, before_receive_id, after_receive_id, result_json)
           VALUES (?, ?, ?, ?, ?)''',
        (result['review_id'], result['day'], result['before']['receive_id'],
         result['after']['receive_id'],
         json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(',', ':'))),
    )


def _load_review(db: sqlite3.Connection, review_id: str) -> dict | None:
    row = db.execute('SELECT result_json FROM row_reviews WHERE review_id = ?',
                     (review_id,)).fetchone()
    return None if row is None else json.loads(row[0])


def read_row_review(db: sqlite3.Connection, review_id: str) -> dict | None:
    require_label(review_id, 'review id')
    with isolated_transaction(db, write=False):
        return _load_review(db, review_id)


def review_rows(
    db: sqlite3.Connection, before_receive_id: str, after_receive_id: str, review_id: str,
) -> dict:
    require_label(before_receive_id, 'receive id')
    require_label(after_receive_id, 'receive id')
    require_label(review_id, 'review id')
    with isolated_transaction(db):
        old = _load_review(db, review_id)
        if old is not None:
            if (old['before']['receive_id'], old['after']['receive_id']) != (
                    before_receive_id, after_receive_id):
                raise ValueError('review id already binds a different delivery pair')
            return old
        before_received = read_received_package(db, before_receive_id)
        after_received = read_received_package(db, after_receive_id)
        if before_received is None or after_received is None:
            raise ValueError('unknown received delivery')
        result = build_review(before_received, after_received, review_id)
        _save_review(db, result)
        return result
