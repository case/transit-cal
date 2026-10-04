"""Load agencies' schedules from a GTFS zip."""

import csv
import io
import re
import zipfile
from collections.abc import Iterator
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REQUIRED = ("agency.txt", "routes.txt", "trips.txt", "stop_times.txt", "stops.txt")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
STOP_TIME_COLUMNS = ("trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time")
_TIME = re.compile(r"(\d{1,3}):([0-5]\d):([0-5]\d)")
_DATE = re.compile(r"\d{8}")


class GtfsError(ValueError):
    """The feed is missing data or holds a value this loader cannot read."""


@dataclass(frozen=True, slots=True)
class Stop:
    stop_id: str
    name: str
    parent_station: str
    description: str
    lat: float | None
    lon: float | None


@dataclass(frozen=True, slots=True)
class Trip:
    trip_id: str
    route_id: str
    service_id: str
    block_id: str


@dataclass(frozen=True, slots=True)
class StopTime:
    stop_id: str
    arrival: int
    departure: int


@dataclass(frozen=True, slots=True)
class ServicePattern:
    weekdays: tuple[bool, ...]
    start: date
    end: date


@dataclass
class Feed:
    """One agency's schedule. Stop times are seconds from the start of the service day."""

    timezone: str
    routes: frozenset[str]
    stops: dict[str, Stop] = field(default_factory=dict)
    trips: dict[str, Trip] = field(default_factory=dict)
    stop_times: dict[str, list[StopTime]] = field(default_factory=dict)
    patterns: dict[str, ServicePattern] = field(default_factory=dict)
    added: dict[date, set[str]] = field(default_factory=dict)
    removed: dict[date, set[str]] = field(default_factory=dict)

    def active_services(self, day: date) -> set[str]:
        """Return the service_ids running on this service day."""
        active = {
            sid
            for sid, p in self.patterns.items()
            if p.start <= day <= p.end and p.weekdays[day.weekday()]
        }
        return (active | self.added.get(day, set())) - self.removed.get(day, set())

    def service_end_date(self) -> date | None:
        """Return the last day any service could run, or None for a feed without service."""
        return max([p.end for p in self.patterns.values()] + list(self.added), default=None)


def parse_time(text: str) -> int:
    """Parse H:MM:SS into seconds. Hours may pass 23 for trips after midnight."""
    m = _TIME.fullmatch(text.strip())
    if not m:
        raise GtfsError(f"bad time {text!r}")
    h, mi, s = map(int, m.groups())
    return h * 3600 + mi * 60 + s


def load(path: Path, agency_ids: AbstractSet[str]) -> dict[str, Feed]:
    """Load each agency's routes, trips, stop times, stops and calendars in one pass.

    Only rows the requested agencies use are parsed. Raises GtfsError for a file that is not a
    zip, text that is not UTF-8, a missing required file, an agency missing from the feed or
    without routes, an unknown timezone, or a value that cannot be read.
    """
    try:
        return _load(path, agency_ids)
    except zipfile.BadZipFile as e:
        raise GtfsError(f"{path.name} is not a readable zip: {e}") from e


def _load(path: Path, agency_ids: AbstractSet[str]) -> dict[str, Feed]:
    with zipfile.ZipFile(path) as zf:
        missing = [name for name in REQUIRED if name not in zf.namelist()]
        if missing:
            raise GtfsError(f"feed has no {', '.join(missing)}")

        timezones = {
            r["agency_id"]: r["agency_timezone"]
            for r in _rows(zf, "agency.txt", "agency_id", "agency_timezone")
            if r["agency_id"] in agency_ids
        }
        if unknown := sorted(agency_ids - timezones.keys()):
            raise GtfsError(f"agency {unknown[0]!r} not in feed")
        for agency, tz in sorted(timezones.items()):
            try:
                ZoneInfo(tz)
            except ZoneInfoNotFoundError, ValueError:
                raise GtfsError(f"agency {agency!r} has unknown timezone {tz!r}") from None

        route_agency = {
            r["route_id"]: r["agency_id"]
            for r in _rows(zf, "routes.txt", "route_id", "agency_id")
            if r["agency_id"] in agency_ids
        }
        if routeless := sorted(agency_ids - set(route_agency.values())):
            raise GtfsError(f"agency {routeless[0]!r} has no routes")

        feeds = {
            a: Feed(tz, frozenset(r for r, ra in route_agency.items() if ra == a))
            for a, tz in timezones.items()
        }
        trip_agency: dict[str, str] = {}
        for r in _rows(zf, "trips.txt", "route_id", "service_id", "trip_id"):
            agency = route_agency.get(r["route_id"])
            if agency:
                trip = Trip(r["trip_id"], r["route_id"], r["service_id"], r.get("block_id", ""))
                feeds[agency].trips[trip.trip_id] = trip
                trip_agency[trip.trip_id] = agency

        sequenced: dict[str, list[tuple[int, StopTime]]] = {}
        for r in _rows(zf, "stop_times.txt", *STOP_TIME_COLUMNS):
            if r["trip_id"] in trip_agency:
                st = StopTime(
                    r["stop_id"], parse_time(r["arrival_time"]), parse_time(r["departure_time"])
                )
                sequenced.setdefault(r["trip_id"], []).append((_int(r["stop_sequence"]), st))
        for trip_id, rows in sequenced.items():
            rows.sort(key=lambda row: row[0])
            feeds[trip_agency[trip_id]].stop_times[trip_id] = [st for _, st in rows]

        stop_rows = {r["stop_id"]: r for r in _rows(zf, "stops.txt", "stop_id")}
        for feed in feeds.values():
            used = {st.stop_id for sts in feed.stop_times.values() for st in sts}
            if unknown := sorted(used - stop_rows.keys()):
                raise GtfsError(f"stop {unknown[0]!r} not in stops.txt")
            used |= {stop_rows[s].get("parent_station", "") for s in used} - {""}
            feed.stops = {s: _stop(stop_rows[s]) for s in used if s in stop_rows}

        service_feeds: dict[str, list[Feed]] = {}
        for feed in feeds.values():
            for sid in {t.service_id for t in feed.trips.values()}:
                service_feeds.setdefault(sid, []).append(feed)
        if "calendar.txt" in zf.namelist():
            for r in _rows(zf, "calendar.txt", "service_id", *WEEKDAYS, "start_date", "end_date"):
                if users := service_feeds.get(r["service_id"]):
                    pattern = _pattern(r)
                    for feed in users:
                        feed.patterns[r["service_id"]] = pattern
        if "calendar_dates.txt" in zf.namelist():
            for r in _rows(zf, "calendar_dates.txt", "service_id", "date", "exception_type"):
                if users := service_feeds.get(r["service_id"]):
                    kind, day = r["exception_type"].strip(), _date(r["date"])
                    if kind not in ("1", "2"):
                        raise GtfsError(f"bad exception_type {kind!r} for {r['service_id']}")
                    for feed in users:
                        target = feed.added if kind == "1" else feed.removed
                        target.setdefault(day, set()).add(r["service_id"])
    return feeds


def _rows(zf: zipfile.ZipFile, name: str, *columns: str) -> Iterator[dict[str, str]]:
    """Yield a file's rows; short rows read as blanks. Raises GtfsError for a missing column."""
    with zf.open(name) as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""), restval="")
        try:
            if missing := [c for c in columns if c not in (reader.fieldnames or [])]:
                raise GtfsError(f"{name} has no {', '.join(missing)} column")
            yield from reader
        except UnicodeDecodeError:
            raise GtfsError(f"{name} is not UTF-8") from None


def _int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise GtfsError(f"bad stop_sequence {text!r}") from None


def _date(text: str) -> date:
    try:
        if not _DATE.fullmatch(text.strip()):
            raise ValueError(text)
        return date.fromisoformat(text.strip())
    except ValueError:
        raise GtfsError(f"bad date {text!r}") from None


def _coordinate(text: str, limit: float) -> float | None:
    if not text.strip():
        return None
    try:
        value = float(text)
    except ValueError:
        raise GtfsError(f"bad coordinate {text!r}") from None
    if not -limit <= value <= limit:  # also rejects nan
        raise GtfsError(f"coordinate {text!r} out of range")
    return value


def _stop(r: dict[str, str]) -> Stop:
    return Stop(
        r["stop_id"],
        r.get("stop_name", ""),
        r.get("parent_station", ""),
        r.get("stop_desc", ""),
        _coordinate(r.get("stop_lat", ""), 90),
        _coordinate(r.get("stop_lon", ""), 180),
    )


def _pattern(r: dict[str, str]) -> ServicePattern:
    flags = []
    for day in WEEKDAYS:
        if r[day] not in ("0", "1"):
            raise GtfsError(f"bad {day} {r[day]!r} for {r['service_id']}")
        flags.append(r[day] == "1")
    start, end = _date(r["start_date"]), _date(r["end_date"])
    if end < start:
        raise GtfsError(f"service {r['service_id']} ends before it starts")
    return ServicePattern(tuple(flags), start, end)
