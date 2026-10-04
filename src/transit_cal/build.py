"""Write every route-direction feed from one GTFS zip and the operator catalog.

Every feed is generated in memory before any file is written, so a failure leaves the output
untouched. Each file is replaced atomically; a whole build is not.
"""

import hashlib
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from transit_cal.catalog import Operator, Route
from transit_cal.gtfs import Feed, load
from transit_cal.ics import FeedSpec, Place, build_calendar, service_datetime
from transit_cal.legs import FROM_HUB, TO_HUB, Leg, build_legs

# The most service days scanned per agency; GTFS feeds normally cover months, not years.
MAX_SCAN_DAYS = 1000


class BuildError(ValueError):
    """The catalog, the GTFS or the arguments cannot produce a complete set of feeds."""


@dataclass(frozen=True, slots=True)
class FeedResult:
    spec: FeedSpec
    file: Path
    departures: int
    last_departure: date
    start: date


def build_feeds(
    gtfs: Path,
    operators: Sequence[Operator],
    out: Path,
    *,
    start: date | None = None,
    days: int,
    stamp: datetime,
    now: datetime | None = None,
) -> list[FeedResult]:
    """Write one .ics per route and direction under out, for GTFS service days start..+days-1.

    Without start, each operator's window starts the day before now's date in its agency's
    timezone, so last night's departures after midnight are kept. Returns the feeds by operator
    slug, then route order, to-hub before from-hub. Raises BuildError for bad arguments,
    duplicate operators or paths, a zip that changes while it is read, a route or route stop
    missing from the GTFS, an agency calendar over MAX_SCAN_DAYS, a window past date.max, or a
    feed with no departures anywhere in the GTFS; nothing is written then. An OSError while
    writing can leave some files replaced and others not.
    """
    if days < 1:
        raise BuildError(f"days must be at least 1, not {days}")
    if stamp.utcoffset() is None:
        raise BuildError(f"stamp {stamp} has no timezone")
    first_day = _first_day(start, now)
    if not operators:
        raise BuildError("no operators to build")
    operators = sorted(operators, key=lambda op: op.slug)
    for attr in ("onestop_id", "gtfs_agency"):
        values = [getattr(op, attr) for op in operators]
        if twice := sorted({v for v in values if values.count(v) > 1}):
            raise BuildError(f"{attr} {twice[0]!r} given twice")
    paths = [s.path for op in operators for r in op.routes for s in _specs(op, r)]
    if twice := sorted({p for p in paths if paths.count(p) > 1}):
        raise BuildError(f"feed path {twice[0]!r} built twice")

    sha = _sha1(gtfs)
    feeds = load(gtfs, {op.gtfs_agency for op in operators})
    if _sha1(gtfs) != sha:
        raise BuildError(f"{gtfs.name} changed while it was read")

    generated: list[tuple[FeedResult, bytes]] = []
    for op in operators:
        feed = feeds[op.gtfs_agency]
        tz = ZoneInfo(feed.timezone)
        provenance = (
            f"511.org regional GTFS, Transitland feed version {sha[:8]}, "
            f"built {stamp.astimezone(tz):%Y-%m-%d}"
        )
        places = _places(feed, op)
        scan = _scan_days(feed, op)
        try:
            window_start = first_day(tz)
            window_end = window_start + timedelta(days=days - 1)
        except OverflowError:
            raise BuildError(f"{op.slug}: a window of {days} days is out of date range") from None
        for route in op.routes:
            if route.gtfs_route not in feed.routes:
                raise BuildError(f"{op.slug}: route {route.gtfs_route!r} is not in the GTFS")
            stops = {route.hub.station, *route.terminals, *op.other_hubs(route)}
            if unknown := sorted(stops - feed.stops.keys()):
                raise BuildError(f"{op.slug}: route {route.slug!r}: unknown stop {unknown[0]!r}")
            try:
                legs = [
                    leg
                    for day in scan
                    for leg in build_legs(
                        feed, day, route.hub.station, route.terminals, op.other_hubs(route)
                    )
                ]
            except ValueError as e:
                raise BuildError(f"{op.slug}: route {route.slug!r}: {e}") from e
            for spec in _specs(op, route):
                ours = [leg for leg in legs if leg.direction == spec.direction]
                if not ours:
                    raise BuildError(f"{spec.path}: no departures in GTFS feed version {sha[:8]}")
                last = max(_local_date(leg, tz) for leg in ours)
                window = [leg for leg in ours if window_start <= leg.service_date <= window_end]
                cal = build_calendar(
                    spec,
                    window,
                    stops=op.stops,
                    places=places,
                    tz=tz,
                    stamp=stamp,
                    last_departure=last,
                    window_end=window_end,
                    provenance=provenance,
                )
                result = FeedResult(spec, out / spec.path, len(set(window)), last, window_start)
                generated.append((result, cal.to_ical()))

    for result, data in generated:
        _write(result.file, data)
    return [result for result, _ in generated]


def _first_day(start: date | None, now: datetime | None) -> Callable[[ZoneInfo], date]:
    """The window's first service day for an agency timezone."""
    if start is not None:
        return lambda tz: start
    if now is None or now.utcoffset() is None:
        raise BuildError(f"now {now} must carry a timezone when no start is given")
    clock = now
    return lambda tz: clock.astimezone(tz).date() - timedelta(days=1)


def _sha1(path: Path) -> str:
    """Transitland names a feed version by the SHA-1 of its zip."""
    with path.open("rb") as f:
        return hashlib.file_digest(f, lambda: hashlib.sha1(usedforsecurity=False)).hexdigest()


def _specs(op: Operator, route: Route) -> list[FeedSpec]:
    return [
        FeedSpec(
            op.onestop_id,
            op.name,
            route.slug,
            route.name,
            direction,
            d.slug,
            d.name,
            route.url,
            page_url=None,
        )
        for direction, d in ((TO_HUB, route.hub.to), (FROM_HUB, route.hub.from_))
    ]


def _places(feed: Feed, op: Operator) -> dict[str, Place]:
    """Each catalog stop present in the GTFS, with catalog title and address over the GTFS ones."""
    places = {}
    for sid, info in op.stops.items():
        if (stop := feed.stops.get(sid)) is not None:
            title = info.place or stop.name
            places[sid] = Place(title, info.address or stop.description, stop.lat, stop.lon)
    return places


def _scan_days(feed: Feed, op: Operator) -> list[date]:
    """Every day from the earliest service start to the last service day, capped."""
    end = feed.service_end_date()
    first = min([p.start for p in feed.patterns.values()] + list(feed.added), default=None)
    if first is None or end is None:
        return []
    span = (end - first).days + 1
    if span > MAX_SCAN_DAYS:
        raise BuildError(
            f"{op.slug}: GTFS calendar spans {span} days ({first} to {end}), "
            f"over the {MAX_SCAN_DAYS}-day limit"
        )
    return [first + timedelta(days=i) for i in range(span)]


def _local_date(leg: Leg, tz: ZoneInfo) -> date:
    return service_datetime(leg.service_date, leg.stops[0].departure, tz).date()


def _write(path: Path, data: bytes) -> None:
    """Replace path with data atomically: a reader sees the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp.chmod(0o644)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
