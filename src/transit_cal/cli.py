"""Command line: `transit-cal build GTFS_ZIP` writes every feed from one GTFS zip."""

import argparse
import os
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from itertools import groupby
from pathlib import Path

from transit_cal.build import BuildError, FeedResult, build_feeds
from transit_cal.catalog import CatalogError, load_operators
from transit_cal.gtfs import GtfsError
from transit_cal.ics import IcsError
from transit_cal.legs import LegError

# An ASCII integer, as `date +%s` prints it: https://reproducible-builds.org/specs/source-date-epoch/
EPOCH = re.compile(r"[0-9]+")
EXPECTED = (BuildError, CatalogError, GtfsError, IcsError, LegError, OSError)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command; returns the exit status. Usage errors exit 2 through argparse."""
    parser = argparse.ArgumentParser(prog="transit-cal")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="write every route-direction .ics feed")
    build.add_argument("gtfs", type=Path, help="GTFS zip")
    build.add_argument("--out", type=Path, default=Path("out"), help="output directory (out)")
    build.add_argument(
        "--start",
        type=date.fromisoformat,
        help="first GTFS service day, YYYY-MM-DD (yesterday in each agency's timezone)",
    )
    build.add_argument("--days", type=_positive, default=60, help="service days to cover (60)")
    build.add_argument(
        "--operators",
        type=Path,
        help="operator catalog directory, for tests and development (the packaged catalog)",
    )
    args = parser.parse_args(argv)

    now = _now()
    try:
        results = build_feeds(
            args.gtfs,
            load_operators(args.operators),
            args.out,
            start=args.start,
            days=args.days,
            stamp=_stamp(os.environ, now),
            now=now,
        )
    except EXPECTED as e:
        print(f"transit-cal: error: {e}", file=sys.stderr)
        return 1
    _summarize(results, args.days, args.out)
    return 0


def _now() -> datetime:
    return datetime.now(UTC)


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {value}")
    return value


def _stamp(environ: Mapping[str, str], now: datetime) -> datetime:
    """DTSTAMP: SOURCE_DATE_EPOCH when set, else now. Raises BuildError for a malformed value."""
    if (value := environ.get("SOURCE_DATE_EPOCH")) is None:
        return now
    if not EPOCH.fullmatch(value):
        raise BuildError(f"SOURCE_DATE_EPOCH {value!r} is not a whole number of seconds")
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except OverflowError, OSError, ValueError:
        raise BuildError(f"SOURCE_DATE_EPOCH {value!r} is out of range") from None


def _summarize(results: list[FeedResult], days: int, out: Path) -> None:
    by_operator = groupby(results, lambda r: (r.spec.onestop_id, r.spec.operator_name, r.start))
    for (_, name, start), feeds in by_operator:
        print(f"{name}: service days {start} to {start + timedelta(days=days - 1)}")
        for r in feeds:
            print(f"{r.departures:7d} departures, last {r.last_departure}  {r.spec.path}")
    print(f"wrote {len(results)} feeds under {out}")


if __name__ == "__main__":
    sys.exit(main())
