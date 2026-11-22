from dataclasses import dataclass

from issue.codec import content_identity, decode_envelope, require_fields
from issue.effects import parse_window
from issue.manifests import InventoryReference, Manifest, parse_manifest
from journal.balances import ZoneBalance, require_balance
from journal.postings import require_cut, require_label

from .acceptance import ReceivedPackage


@dataclass(frozen=True, slots=True)
class EffectAudit:
    zone: str
    rows: int
    litres: int
    covered: bool
    count_id: str
    cut: int
    origin: str
    count_identity: str


@dataclass(frozen=True, slots=True)
class PostingAudit:
    seq: int
    kind: str
    batch_id: str
    lines: tuple[tuple[str, int], ...]
    effects: tuple[EffectAudit, ...]


@dataclass(frozen=True, slots=True)
class Reconciliation:
    day: str
    issue_id: str
    target: int
    balances: tuple[ZoneBalance, ...]
    counts: tuple[InventoryReference, ...]
    audit: tuple[PostingAudit, ...]


def _match_counts(value: object, manifest: Manifest) -> dict[str, ZoneBalance]:
    if type(value) is not list or len(value) != len(manifest.inventory):
        raise ValueError('received inventories are incomplete')
    expected = {reference.count_id: reference for reference in manifest.inventory}
    balances = {}
    for item in value:
        data = require_fields(item, {'day', 'zone', 'count_id', 'rows', 'litres', 'origin'},
                              'received inventory')
        count_id = data['count_id']
        require_label(count_id, 'count id')
        reference = expected.pop(count_id, None)
        if reference is None:
            raise ValueError(f'unselected or repeated inventory {count_id}')
        if (data['day'] != manifest.day or data['zone'] != reference.zone
                or data['origin'] != reference.origin or content_identity(data) != reference.identity):
            raise ValueError(f'received inventory does not match reference {count_id}')
        balance = ZoneBalance(reference.zone, data['rows'], data['litres'])
        require_balance(balance)
        balances[reference.zone] = balance
    return balances


def reconcile_day(received: ReceivedPackage, target: int) -> Reconciliation:
    require_cut(target)
    envelope = decode_envelope(received.raw)
    if envelope.identity != received.identity:
        raise ValueError('received version identity mismatch')
    body = require_fields(envelope.body, {'manifest', 'counts', 'postings'}, 'handoff body')
    manifest = parse_manifest(body['manifest'])
    if (manifest.day, manifest.issue_id, manifest.target) != (received.day, received.issue_id, target):
        raise ValueError('received delivery or target does not match the requested restoration')
    balances = _match_counts(body['counts'], manifest)
    postings = parse_window(body['postings'], manifest.day, manifest.start, target)
    references = {reference.zone: reference for reference in manifest.inventory}
    audit = []
    for posting in postings:
        effects = []
        for effect in posting.effects:
            reference = references[effect.zone]
            covered = posting.seq <= reference.cut
            if not covered:
                balances[effect.zone] = balances[effect.zone].apply(effect)
            effects.append(EffectAudit(effect.zone, effect.rows, effect.litres, covered,
                                       reference.count_id, reference.cut, reference.origin,
                                       reference.identity))
        audit.append(PostingAudit(posting.seq, posting.kind, posting.batch_id, posting.lines,
                                  tuple(effects)))
    return Reconciliation(manifest.day, manifest.issue_id, target,
                          tuple(balances[reference.zone] for reference in manifest.inventory),
                          manifest.inventory, tuple(audit))
