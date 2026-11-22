from dataclasses import dataclass
import sqlite3


@dataclass(frozen=True, slots=True)
class IssuedPackage:
    issue_id: str
    day: str
    start: int
    target: int
    identity: str
    raw: bytes


def save_issued_package(db: sqlite3.Connection, package: IssuedPackage) -> None:
    db.execute(
        '''INSERT INTO issued_packages (issue_id, day, start, target, identity, raw)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (package.issue_id, package.day, package.start, package.target, package.identity, package.raw),
    )


def read_issued_package(db: sqlite3.Connection, issue_id: str) -> IssuedPackage | None:
    row = db.execute(
        'SELECT day, start, target, identity, raw FROM issued_packages WHERE issue_id = ?',
        (issue_id,),
    ).fetchone()
    return None if row is None else IssuedPackage(issue_id, *row)
