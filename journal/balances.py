from dataclasses import dataclass
import sqlite3

from .postings import ZoneEffect, posting_exists, read_postings, require_contiguous, require_day

ZONES = ('east', 'west')


@dataclass(frozen=True, slots=True)
class ZoneBalance:
    zone: str
    rows: int
    litres: int

    def apply(self, effect: ZoneEffect) -> 'ZoneBalance':
        if effect.zone != self.zone:
            raise ValueError('effect zone does not match balance zone')
        result = ZoneBalance(self.zone, self.rows + effect.rows, self.litres + effect.litres)
        require_balance(result)
        return result


def require_zone(zone: object) -> None:
    if type(zone) is not str or zone not in ZONES:
        raise ValueError('unknown accounting zone')


def require_balance(balance: ZoneBalance) -> None:
    if type(balance.rows) is not int or type(balance.litres) is not int:
        raise ValueError('balance measurements must be integers')
    if balance.rows < 0 or balance.litres < 0 or (balance.rows == 0 and balance.litres != 0):
        raise ValueError('inconsistent zone balance')


def balance_at_cut(db: sqlite3.Connection, day: str, zone: str, cut: int) -> ZoneBalance:
    require_day(day)
    require_zone(zone)
    if not posting_exists(db, day, cut):
        raise ValueError('unknown formal cut for day')
    postings = read_postings(db, day, 0, cut)
    require_contiguous(postings, 0, cut)
    balance = ZoneBalance(zone, 0, 0)
    for posting in postings:
        for effect in posting.effects:
            if effect.zone == zone:
                balance = balance.apply(effect)
    return balance
