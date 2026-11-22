from contextlib import closing
from dataclasses import dataclass
import json
import sqlite3

from intake import BatchRequest, ReadingInput, submit
from inventory import freeze
from issue import IssuedPackage, issue_pack
from journal import correct_litres, initialize_source, read_current_revision, transfer
from restore import (
    initialize_isolated, receive_pack, restore_day, review_rows,
)

DAY = "2026-04-12"
BATCH = "station"


@dataclass(frozen=True)
class Deliveries:
    before: IssuedPackage
    after: IssuedPackage
    staggered: IssuedPackage
    alternate: IssuedPackage


def trim(source, through):
    """Simulate retention removing formal postings, as in the V1 regression."""
    with source:
        source.execute("DELETE FROM postings WHERE day = ? AND seq <= ?", (DAY, through))


def build(source, exchange, freeze_count=freeze, issue_delivery=issue_pack):
    """Create real inputs using the supplied freeze and issue entry points."""
    initialize_source(source)
    initial = submit(source, BatchRequest(DAY, BATCH, (
        ReadingInput("a", "east", 5), ReadingInput("b", "west", 5),
    )))
    freeze_count(source, DAY, "east", "east-start", initial.posting_seq)
    freeze_count(source, DAY, "west", "west-start", initial.posting_seq)

    first = read_current_revision(source, DAY, BATCH, "a")
    middle = (transfer(source, DAY, first, "west", 5) if exchange
              else correct_litres(source, DAY, first, 5))
    freeze_count(source, DAY, "west", "west-middle", middle.seq)
    freeze_count(source, DAY, "west", "west-spare", middle.seq)
    second = read_current_revision(source, DAY, BATCH, "b")
    last = (transfer(source, DAY, second, "east", 5) if exchange
            else correct_litres(source, DAY, second, 5))
    freeze_count(source, DAY, "east", "east-end", last.seq)
    freeze_count(source, DAY, "west", "west-end", last.seq)

    trim(source, initial.posting_seq)
    staggered = issue_delivery(source, DAY, "staggered", ("east-start", "west-middle"), last.seq)
    alternate = issue_delivery(source, DAY, "alternate", ("east-start", "west-spare"), last.seq)
    before = issue_delivery(source, DAY, "before", ("east-start", "west-start"), initial.posting_seq)
    trim(source, last.seq)
    after = issue_delivery(source, DAY, "after", ("east-end", "west-end"), last.seq)
    return Deliveries(before, after, staggered, alternate)


def run_v1(exchange):
    with closing(sqlite3.connect(":memory:")) as source:
        deliveries = build(source, exchange)
        positions = [[row.line_id, row.zone, row.litres, row.revision] for row in (
            read_current_revision(source, DAY, BATCH, "a"),
            read_current_revision(source, DAY, BATCH, "b"),
        )]
    figures = {}
    with closing(sqlite3.connect(":memory:")) as isolated:
        initialize_isolated(isolated)
        for name in ("before", "after", "staggered"):
            package = getattr(deliveries, name)
            receive_pack(isolated, name, package.raw)
            result = restore_day(isolated, name, package.target, name)
            figures[name] = [[b.zone, b.rows, b.litres] for b in result.reconciliation.balances]
    return deliveries, positions, figures


def main():
    moved, moved_rows, moved_figures = run_v1(True)
    corrected, corrected_rows, corrected_figures = run_v1(False)
    figures = [["east", 1, 5], ["west", 1, 5]]
    expected = {name: figures for name in ("before", "after", "staggered")}
    equal_bytes = moved.before.raw == corrected.before.raw and moved.after.raw == corrected.after.raw
    if (not equal_bytes or moved_figures != expected or corrected_figures != expected
            or moved_rows != [["a", "west", 5, 2], ["b", "east", 5, 2]]
            or corrected_rows != [["a", "east", 5, 2], ["b", "west", 5, 2]]):
        raise SystemExit("unexpected V1 fixture results")
    print(json.dumps({"same_v1_endpoint_bytes": equal_bytes, "restored": moved_figures,
                      "transfers": moved_rows, "corrections": corrected_rows}, sort_keys=True))
    run_v2(True)
    run_v2(False)


def run_v2(exchange):
    """Build the same inputs as V2 and review both rows end to end."""
    with closing(sqlite3.connect(":memory:")) as source:
        deliveries = build(
            source, exchange,
            freeze_count=lambda *args: freeze(*args, row_evidence=True),
            issue_delivery=lambda *args: issue_pack(*args, row_evidence=True),
        )
        final = {row.line_id: [row.zone, row.litres, row.revision] for row in (
            read_current_revision(source, DAY, BATCH, "a"),
            read_current_revision(source, DAY, BATCH, "b"),
        )}
    initial = {"a": ["east", 5, 1], "b": ["west", 5, 1]}
    reviews = {}
    with closing(sqlite3.connect(":memory:")) as isolated:
        initialize_isolated(isolated)
        for name in ("before", "after", "staggered", "alternate"):
            receive_pack(isolated, name, getattr(deliveries, name).raw)
        for before, after, review_id in (("before", "after", "end"),
                                         ("before", "staggered", "middle"),
                                         ("before", "alternate", "spare")):
            review = review_rows(isolated, before, after, review_id)
            reviews[review_id] = [[row["line_id"],
                                   row["before"] and [row["before"]["zone"],
                                                      row["before"]["litres"],
                                                      row["before"]["revision"]],
                                   [row["after"]["zone"], row["after"]["litres"],
                                    row["after"]["revision"]],
                                   row["changes"]] for row in review["rows"]]
    expected_changes = ["moved"] if exchange else ["same"]
    expected = [[line_id, initial[line_id], final[line_id], expected_changes]
                for line_id in ("a", "b")]
    for review_id in ("end", "middle", "spare"):
        if reviews[review_id] != expected:
            raise SystemExit("unexpected V2 row review " + json.dumps(
                {"review_id": review_id, "actual": reviews[review_id]}, sort_keys=True))
    middle = reviews["middle"]
    if middle[0][1][0] != "east" or middle[1][1][0] != "west":
        raise SystemExit("unexpected staggered evidence " + json.dumps(middle))
    print(json.dumps({"v2_endpoint_rows": expected, "reviews": reviews}, sort_keys=True))


if __name__ == "__main__":
    main()
