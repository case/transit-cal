"""One feed's rider legs as an iCalendar feed, with UIDs that survive rebuilds and moves."""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event
from icalendar.enums import TRANSP
from icalendar.prop import vDuration, vUri

from transit_cal.catalog import StopInfo
from transit_cal.legs import FROM_HUB, TO_HUB, Leg

# Frozen: every published UID derives from this namespace and these key suffixes.
NAMESPACE = uuid.UUID("4593417e-442d-4f36-9a46-ac40000e4215")
DATA_ENDS = "#data-ends"
SERVICE_ENDED = "#service-ended"

PRODID = "-//transit-cal//transit-cal//EN"
REFRESH = timedelta(days=1)
ATTRIBUTION = "Schedule data provided by 511.org (https://511.org) via Transitland"
MAP_RADIUS_METERS = 100


class IcsError(ValueError):
    """A feed's legs cannot be published as given, such as two legs sharing a UID key."""


@dataclass(frozen=True, slots=True)
class Place:
    title: str
    address: str
    lat: float | None
    lon: float | None

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.address}" if self.address else self.title


@dataclass(frozen=True, slots=True)
class FeedSpec:
    onestop_id: str
    operator_name: str
    route_slug: str
    route_name: str
    direction: str
    direction_slug: str
    direction_name: str
    operator_url: str
    page_url: str | None

    @property
    def key(self) -> str:
        """The feed's permanent identity: its URL path without the extension."""
        return f"{self.onestop_id}/{self.route_slug}-{self.direction_slug}"

    @property
    def path(self) -> str:
        return f"{self.key}.ics"

    @property
    def calendar_name(self) -> str:
        return f"{self.operator_name} · {self.route_name} · {self.direction_name}"


def uid(key: str) -> str:
    return str(uuid.uuid5(NAMESPACE, key))


def _gtfs_time(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def leg_key(spec: FeedSpec, leg: Leg) -> str:
    """Feed key, service date, first departure and the terminal (non-hub) end's stop_id."""
    terminal = leg.stops[0] if leg.direction == TO_HUB else leg.stops[-1]
    departure = _gtfs_time(leg.stops[0].departure)
    return f"{spec.key}/{leg.service_date:%Y%m%d}/{departure}/{terminal.stop_id}"


def service_datetime(day: date, seconds: int, tz: ZoneInfo) -> datetime:
    """GTFS stop time to local datetime; GTFS counts from noon minus 12h, not midnight."""
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=tz).astimezone(UTC)
    return (noon - timedelta(hours=12) + timedelta(seconds=seconds)).astimezone(tz)


def add_location(event: Event, place: Place) -> None:
    """Set LOCATION, GEO, and Apple's undocumented structured location, in Apple's own layout."""
    event.add("LOCATION", place.text)
    if place.lat is None or place.lon is None:
        return
    event.add("GEO", (place.lat, place.lon))
    # RFC 5545 section 3.2 forbids DQUOTE inside parameter values.
    params = {"VALUE": "URI"}
    if place.address:
        params["X-ADDRESS"] = place.address.replace('"', "'")
    params["X-APPLE-RADIUS"] = str(MAP_RADIUS_METERS)
    params["X-APPLE-REFERENCEFRAME"] = "1"
    params["X-TITLE"] = place.title.replace('"', "'")
    event.add(
        "X-APPLE-STRUCTURED-LOCATION", vUri(f"geo:{place.lat},{place.lon}"), parameters=params
    )


def _show(leg: Leg) -> str:
    calls = ", ".join(
        f"{s.stop_id} {_gtfs_time(s.arrival)}-{_gtfs_time(s.departure)}" for s in leg.stops
    )
    return f"{leg.service_date} [{calls}]"


def _keyed(spec: FeedSpec, legs: Iterable[Leg]) -> list[tuple[str, Leg]]:
    """Each distinct leg with its UID key, in time order. Identical duplicates collapse."""
    if spec.direction not in (TO_HUB, FROM_HUB):
        raise IcsError(f"{spec.path}: unknown direction {spec.direction!r}")
    by_key: dict[str, Leg] = {}
    for leg in legs:
        if leg.direction != spec.direction:
            raise IcsError(
                f"{spec.path}: {_show(leg)} runs {leg.direction}, the feed runs {spec.direction}"
            )
        key = leg_key(spec, leg)
        if (seen := by_key.setdefault(key, leg)) != leg:
            raise IcsError(f"{spec.path}: {key} names two legs: {_show(seen)} and {_show(leg)}")
    return sorted(
        by_key.items(), key=lambda kj: (kj[1].service_date, kj[1].stops[0].departure, kj[0])
    )


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _describe(spec: FeedSpec, leg: Leg, stops: Mapping[str, StopInfo], tz: ZoneInfo) -> str:
    lines = []
    last = len(leg.stops) - 1
    for i, s in enumerate(leg.stops):
        info = stops[s.stop_id]
        name = f"{info.name} ({info.gate})" if info.gate else info.name
        verb, secs = ("Arrive", s.arrival) if i == last else ("Depart", s.departure)
        lines.append(f"{verb} {name} {_clock(service_datetime(leg.service_date, secs, tz))}")
    lines += ["", f"{spec.operator_name} schedule: {spec.operator_url}"]
    if spec.page_url is not None:
        lines.append(f"Schedule details: {spec.page_url}")
    lines.append(ATTRIBUTION)
    return "\n".join(lines)


def build_calendar(
    spec: FeedSpec,
    legs: Iterable[Leg],
    *,
    stops: Mapping[str, StopInfo],
    places: Mapping[str, Place],
    tz: ZoneInfo,
    stamp: datetime,
    last_departure: date,
    window_end: date,
    provenance: str,
) -> Calendar:
    """Build one feed, with an all-day marker after last_departure when that is before window_end.

    stamp must be timezone-aware; it is written as DTSTAMP in UTC on every event. Raises
    IcsError for a naive stamp, an unknown or mismatched direction, a stop missing from stops,
    two different legs sharing a UID key, or events too close to date.min or date.max.
    """
    if stamp.utcoffset() is None:
        raise IcsError(f"{spec.path}: stamp {stamp} has no timezone")
    keyed = _keyed(spec, legs)
    for _, leg in keyed:
        if missing := sorted({s.stop_id for s in leg.stops} - stops.keys()):
            raise IcsError(f"{spec.path}: stop {missing[0]!r} is not in the catalog")

    cal = Calendar.new(
        prodid=PRODID,
        uid=uid(spec.key),
        name=spec.calendar_name,
        description=(
            f"{spec.route_name} departures: {spec.direction_name}. "
            f"Last scheduled departure: {last_departure:%Y-%m-%d}. "
            f"Built from {provenance}. {ATTRIBUTION}. {spec.operator_url}"
        ),
        url=spec.operator_url,
    )
    # icalendar 7.3 drops RFC 7986's required VALUE=DURATION, so set it explicitly.
    cal.add("REFRESH-INTERVAL", vDuration(REFRESH), parameters={"VALUE": "DURATION"})
    cal.add("X-PUBLISHED-TTL", vDuration(REFRESH))
    cal.add("X-WR-TIMEZONE", str(tz))

    days: list[date] = []
    for key, leg in keyed:
        origin, dest = leg.stops[0], leg.stops[-1]
        start = service_datetime(leg.service_date, origin.departure, tz)
        end = service_datetime(leg.service_date, dest.arrival, tz)
        days += [start.date(), end.date()]
        event = Event.new(
            uid=uid(key),
            stamp=stamp,
            start=start,
            end=end,
            summary=f"{stops[origin.stop_id].name} → {stops[dest.stop_id].name}",
            description=_describe(spec, leg, stops, tz),
            url=spec.operator_url,
            transparency=TRANSP.TRANSPARENT,
            sequence=0,
        )
        add_location(
            event, places.get(origin.stop_id) or Place(stops[origin.stop_id].name, "", None, None)
        )
        cal.add_component(event)

    if last_departure < window_end:
        gap = last_departure + timedelta(days=1)
        # %-d is not portable (it fails on Windows), so the day is formatted separately.
        d = last_departure
        short, long = f"{d:%b} {d.day}", f"{d:%B} {d.day}, {d.year}"
        cal.add_component(
            Event.new(
                uid=uid(spec.key + DATA_ENDS),
                stamp=stamp,
                start=gap,
                end=gap + timedelta(days=1),
                summary=f"No departures scheduled after {short} for {spec.route_name}",
                description=(
                    "The last scheduled departure in the data we have is "
                    f"{long}. "
                    f"Check the operator for newer schedules. {spec.operator_url}"
                ),
                url=spec.operator_url,
                transparency=TRANSP.TRANSPARENT,
                sequence=0,
            )
        )
    if days:
        # icalendar's default range ends in 2038, so cover the events' own dates instead.
        try:
            first, last = min(days) - timedelta(days=1), max(days) + timedelta(days=1)
            cal.add_missing_timezones(first_date=first, last_date=last)
        except OverflowError:
            span = f"{min(days)} to {max(days)}"
            raise IcsError(f"{spec.path}: events from {span} are out of range") from None
    return cal
