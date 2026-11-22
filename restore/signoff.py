from dataclasses import asdict, dataclass
import json
import sqlite3

from issue.manifests import InventoryReference
from journal.balances import ZoneBalance
from journal.postings import require_cut, require_label

from .acceptance import read_received_package
from .reconcile import EffectAudit, PostingAudit, Reconciliation, reconcile_day
from .store import isolated_transaction


@dataclass(frozen=True, slots=True)
class RestorationReceipt:
    restore_id: str
    receive_id: str
    day: str
    issue_id: str
    target: int
    content_identity: str


@dataclass(frozen=True, slots=True)
class RestoredDay:
    reconciliation: Reconciliation
    receipt: RestorationReceipt


def save_restored_day(db: sqlite3.Connection, restored: RestoredDay) -> None:
    result = restored.reconciliation
    receipt = restored.receipt
    db.execute(
        '''INSERT INTO restore_runs (restore_id, receive_id, day, issue_id, target)
           VALUES (?, ?, ?, ?, ?)''',
        (receipt.restore_id, receipt.receive_id, result.day, result.issue_id, result.target),
    )
    db.executemany(
        '''INSERT INTO restore_balances (restore_id, zone, rows, litres, inventory_json)
           VALUES (?, ?, ?, ?, ?)''',
        ((receipt.restore_id, balance.zone, balance.rows, str(balance.litres), json.dumps(asdict(count)))
         for balance, count in zip(result.balances, result.counts, strict=True)),
    )
    db.executemany(
        'INSERT INTO restore_audit (restore_id, seq, posting_json) VALUES (?, ?, ?)',
        ((receipt.restore_id, posting.seq, json.dumps(asdict(posting))) for posting in result.audit),
    )
    db.execute('INSERT INTO restore_receipts (restore_id, content_identity) VALUES (?, ?)',
               (receipt.restore_id, receipt.content_identity))


def _load_restored_day(db: sqlite3.Connection, restore_id: str) -> RestoredDay | None:
    header = db.execute(
        '''SELECT r.receive_id, r.day, r.issue_id, r.target, s.content_identity
           FROM restore_runs AS r JOIN restore_receipts AS s USING (restore_id)
           WHERE r.restore_id = ?''',
        (restore_id,),
    ).fetchone()
    if header is None:
        return None
    receive_id, day, issue_id, target, identity = header
    balances = []
    counts = []
    for zone, rows, litres, reference in db.execute(
        '''SELECT zone, rows, litres, inventory_json FROM restore_balances
           WHERE restore_id = ? ORDER BY zone''',
        (restore_id,),
    ):
        balances.append(ZoneBalance(zone, rows, int(litres)))
        counts.append(InventoryReference(**json.loads(reference)))
    audit = []
    for (record,) in db.execute(
        'SELECT posting_json FROM restore_audit WHERE restore_id = ? ORDER BY seq', (restore_id,),
    ):
        posting = json.loads(record)
        audit.append(PostingAudit(posting['seq'], posting['kind'], posting['batch_id'],
                                  tuple(tuple(line) for line in posting['lines']),
                                  tuple(EffectAudit(**effect) for effect in posting['effects'])))
    reconciliation = Reconciliation(day, issue_id, target, tuple(balances), tuple(counts), tuple(audit))
    receipt = RestorationReceipt(restore_id, receive_id, day, issue_id, target, identity)
    return RestoredDay(reconciliation, receipt)


def restore_day(
    db: sqlite3.Connection, receive_id: str, target: int, restore_id: str,
) -> RestoredDay:
    require_label(restore_id, 'restore id')
    require_label(receive_id, 'receive id')
    require_cut(target)
    with isolated_transaction(db):
        old = _load_restored_day(db, restore_id)
        if old is not None:
            if (old.receipt.receive_id, old.receipt.target) != (receive_id, target):
                raise ValueError('restore id already binds a different delivery or target')
            return old
        received = read_received_package(db, receive_id)
        if received is None:
            raise ValueError('unknown received delivery')
        result = reconcile_day(received, target)
        receipt = RestorationReceipt(restore_id, receive_id, result.day, result.issue_id,
                                     result.target, received.identity)
        restored = RestoredDay(result, receipt)
        save_restored_day(db, restored)
        return restored
