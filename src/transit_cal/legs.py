"""Rider legs between a hub and a route's terminals, rebuilt from each block's runs.

GTFS trips are how an operator schedules vehicles, not how riders read the timetable. A
block's consecutive trips are joined into a run, the run is cut at every hub, and each
piece gives a leg from the hub to the terminals and one back. A leg can span trips, as a
GTFS in-seat transfer does.
"""

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from transit_cal.gtfs import Feed, Trip

TO_HUB = "to-hub"
FROM_HUB = "from-hub"
# The longest wait at a stop that still joins two trips into one run.
# The Vallejo route waits 20 minutes on weekends.
MAX_DWELL = 20 * 60


class LegError(ValueError):
    """A built leg breaks a model invariant, such as visiting one stop twice."""


@dataclass(frozen=True, slots=True)
class LegStop:
    stop_id: str
    arrival: int
    departure: int


@dataclass(frozen=True, slots=True)
class Leg:
    """One rider's ride on one vehicle, from boarding to alighting."""

    service_date: date
    direction: str
    stops: tuple[LegStop, ...]


def hub_stops(feed: Feed, station: str) -> set[str]:
    """Return a station and every stop whose parent it is."""
    return {s.stop_id for s in feed.stops.values() if s.parent_station == station} | {station}


def build_legs(
    feed: Feed,
    day: date,
    hub: str,
    terminals: AbstractSet[str],
    other_hubs: AbstractSet[str] = frozenset(),
) -> list[Leg]:
    """Return the legs between the hub and the terminals on one service day.

    Runs are also cut at other_hubs, the operator's other hub stations. Raises ValueError for
    an empty or unknown terminal, an unknown hub, or a terminal inside a hub, and LegError
    when a leg would visit a stop twice or go back in time.
    """
    if not terminals:
        raise ValueError("no terminals")
    if unknown := sorted({hub, *other_hubs, *terminals} - feed.stops.keys()):
        raise ValueError(f"unknown stop {unknown[0]!r}")
    ours = hub_stops(feed, hub)
    cuts = ours.union(*(hub_stops(feed, h) for h in other_hubs))
    if inside := sorted(terminals & cuts):
        raise ValueError(f"terminal {inside[0]!r} is part of a hub")

    legs = []
    for block, run in _runs(feed, day):
        ends = [-1, *(i for i, s in enumerate(run) if s.stop_id in cuts), len(run)]
        for a, b in pairwise(ends):
            piece = [s.stop_id for s in run[a + 1 : b]]
            calls = [i for i, sid in enumerate(piece, a + 1) if sid in terminals]
            if not calls:
                continue
            if a >= 0 and run[a].stop_id in ours:
                # Alight at the last terminal call not already passed on the way.
                last = max(i for i in calls if run[i].stop_id not in piece[: i - a - 1])
                legs.append(_leg(run[a : last + 1], day, FROM_HUB, block))
            if b < len(run) and run[b].stop_id in ours:
                # Board at the first terminal call not passed again before the hub.
                first = min(i for i in calls if run[i].stop_id not in piece[i - a :])
                legs.append(_leg(run[first : b + 1], day, TO_HUB, block))
    legs.sort(key=lambda j: (j.direction, j.stops[0].departure))
    return legs


def _runs(feed: Feed, day: date) -> list[tuple[str, list[LegStop]]]:
    """Each run on the day, with its block: consecutive trips joined at a shared stop."""
    active = feed.active_services(day)
    blocks: dict[tuple[str, str], list[Trip]] = {}
    for trip in feed.trips.values():
        if trip.service_id in active and feed.stop_times.get(trip.trip_id):
            key = ("block", trip.block_id) if trip.block_id else ("trip", trip.trip_id)
            blocks.setdefault(key, []).append(trip)

    runs = []
    for (_, block), trips in blocks.items():
        trips.sort(key=lambda trip: feed.stop_times[trip.trip_id][0].departure)
        joined: list[list[LegStop]] = []
        for trip in trips:
            stops = [
                LegStop(s.stop_id, s.arrival, s.departure) for s in feed.stop_times[trip.trip_id]
            ]
            prev = joined[-1][-1] if joined else None
            if (
                prev
                and prev.stop_id == stops[0].stop_id
                and 0 <= stops[0].departure - prev.arrival <= MAX_DWELL
            ):
                joined[-1][-1] = LegStop(prev.stop_id, prev.arrival, stops[0].departure)
                joined[-1].extend(stops[1:])
            else:
                joined.append(stops)
        runs.extend((block, run) for run in joined)
    return runs


def _leg(stops: list[LegStop], day: date, direction: str, block: str) -> Leg:
    """Board at the first stop's departure and alight at the last stop's arrival."""
    ids = [s.stop_id for s in stops]
    if len(ids) != len(set(ids)):
        raise LegError(f"{day} block {block}: leg visits a stop twice: {', '.join(ids)}")
    first, last = stops[0], stops[-1]
    board = LegStop(first.stop_id, first.departure, first.departure)
    alight = LegStop(last.stop_id, last.arrival, last.arrival)
    calls = (board, *stops[1:-1], alight)
    clock = board.departure
    for s in calls[1:]:
        if not clock <= s.arrival <= s.departure:
            raise LegError(f"{day} block {block}: leg goes back in time at {s.stop_id}")
        clock = s.departure
    return Leg(day, direction, calls)
