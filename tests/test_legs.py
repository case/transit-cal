from datetime import date

import pytest

from transit_cal.gtfs import Feed, ServicePattern, Stop, StopTime, Trip
from transit_cal.legs import (
    FROM_HUB,
    TO_HUB,
    Leg,
    LegError,
    LegStop,
    build_legs,
)

DAY = date(2026, 9, 29)  # a Tuesday
# "hub" and SSF (another route's hub) are stations with gate stops.
STOPS = {
    "hub": "",
    "gateF": "hub",
    "gateG": "hub",
    "SSF": "",
    "ssfGate": "SSF",
    "ALA": "",
    "OAK": "",
    "HB": "",
}

Call = tuple[str, str, str]  # stop, arrive, depart


def t(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


def feed(*trips: tuple[str, list[Call]]) -> Feed:
    """A weekday feed of (block_id, calls) trips."""
    f = Feed("America/Los_Angeles", frozenset({"R"}))
    f.stops = {s: Stop(s, s, parent, "", None, None) for s, parent in STOPS.items()}
    f.patterns["WK"] = ServicePattern(
        (True,) * 5 + (False,) * 2, date(2026, 6, 29), date(2026, 11, 1)
    )
    for n, (block, calls) in enumerate(trips):
        trip_id = f"t{n}"
        f.trips[trip_id] = Trip(trip_id, "R", "WK", block)
        f.stop_times[trip_id] = [StopTime(s, t(a), t(d)) for s, a, d in calls]
    return f


def shape(legs: list[Leg]) -> list[tuple[str, list[tuple[str, str]]]]:
    """Each leg as (direction, [(stop, departure)]), ending with the arrival."""

    def hhmm(seconds: int) -> str:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}"

    out = []
    for leg in legs:
        stops = [(s.stop_id, hhmm(s.departure)) for s in leg.stops[:-1]]
        last = leg.stops[-1]
        out.append((leg.direction, [*stops, (last.stop_id, hhmm(last.arrival))]))
    return sorted(out)


LOOP = feed(
    ("B1", [("gateG", "9:00", "9:00"), ("ALA", "9:20", "9:25"), ("OAK", "9:35", "9:40")]),
    ("B1", [("OAK", "9:40", "9:40"), ("gateF", "10:00", "10:00")]),
)


def test_round_trip_through_two_terminals_keeps_both() -> None:
    assert shape(build_legs(LOOP, DAY, "hub", {"ALA", "OAK"})) == [
        (FROM_HUB, [("gateG", "9:00"), ("ALA", "9:25"), ("OAK", "9:35")]),
        (TO_HUB, [("ALA", "9:25"), ("OAK", "9:40"), ("gateF", "10:00")]),
    ]


def test_legs_board_at_departure_and_alight_at_arrival() -> None:
    outbound, inbound = sorted(build_legs(LOOP, DAY, "hub", {"ALA"}), key=lambda j: j.direction)
    assert (outbound.direction, outbound.stops[0], outbound.stops[-1]) == (
        FROM_HUB,
        LegStop("gateG", t("9:00"), t("9:00")),
        LegStop("ALA", t("9:20"), t("9:20")),
    )
    assert (inbound.direction, inbound.stops[0]) == (
        TO_HUB,
        LegStop("ALA", t("9:25"), t("9:25")),
    )


def test_single_terminals_each_get_their_own_legs() -> None:
    assert shape(build_legs(LOOP, DAY, "hub", {"ALA"})) == [
        (FROM_HUB, [("gateG", "9:00"), ("ALA", "9:20")]),
        (TO_HUB, [("ALA", "9:25"), ("OAK", "9:40"), ("gateF", "10:00")]),
    ]
    assert shape(build_legs(LOOP, DAY, "hub", {"OAK"})) == [
        (FROM_HUB, [("gateG", "9:00"), ("ALA", "9:25"), ("OAK", "9:35")]),
        (TO_HUB, [("OAK", "9:40"), ("gateF", "10:00")]),
    ]


def test_one_way_trip_past_the_terminal_gives_only_the_outbound_leg() -> None:
    f = feed(
        ("B1", [("gateF", "17:35", "17:35"), ("OAK", "18:05", "18:10"), ("HB", "18:50", "18:50")])
    )
    assert shape(build_legs(f, DAY, "hub", {"OAK"})) == [
        (FROM_HUB, [("gateF", "17:35"), ("OAK", "18:05")]),
    ]
    assert shape(build_legs(f, DAY, "hub", {"HB"})) == [
        (FROM_HUB, [("gateF", "17:35"), ("OAK", "18:10"), ("HB", "18:50")]),
    ]


INBOUND_REPEAT = feed(
    ("B1", [("ALA", "9:00", "9:00"), ("OAK", "9:10", "9:15"), ("ALA", "9:25", "9:30")]),
    ("B1", [("ALA", "9:30", "9:30"), ("gateG", "10:00", "10:00")]),
)
OUTBOUND_REPEAT = feed(
    ("B1", [("gateG", "9:00", "9:00"), ("ALA", "9:30", "9:35"), ("OAK", "9:45", "9:50")]),
    ("B1", [("OAK", "9:50", "9:50"), ("ALA", "10:00", "10:00")]),
)


def test_terminal_called_twice_boards_at_the_later_call() -> None:
    assert shape(build_legs(INBOUND_REPEAT, DAY, "hub", {"ALA"})) == [
        (TO_HUB, [("ALA", "9:30"), ("gateG", "10:00")]),
    ]
    assert shape(build_legs(INBOUND_REPEAT, DAY, "hub", {"ALA", "OAK"})) == [
        (TO_HUB, [("OAK", "9:15"), ("ALA", "9:30"), ("gateG", "10:00")]),
    ]


def test_terminal_called_twice_alights_at_the_earlier_call() -> None:
    assert shape(build_legs(OUTBOUND_REPEAT, DAY, "hub", {"ALA"})) == [
        (FROM_HUB, [("gateG", "9:00"), ("ALA", "9:30")]),
    ]
    assert shape(build_legs(OUTBOUND_REPEAT, DAY, "hub", {"ALA", "OAK"})) == [
        (FROM_HUB, [("gateG", "9:00"), ("ALA", "9:35"), ("OAK", "9:45")]),
    ]


def test_another_routes_hub_cuts_the_run() -> None:
    f = feed(
        (
            "B1",
            [
                ("gateG", "9:00", "9:00"),
                ("OAK", "9:25", "9:30"),
                ("ssfGate", "10:00", "10:10"),
                ("HB", "10:30", "10:35"),
                ("gateF", "11:00", "11:00"),
            ],
        )
    )
    # Without the cut, a leg would run back from Oakland through South San Francisco.
    assert shape(build_legs(f, DAY, "hub", {"OAK"}, other_hubs={"SSF"})) == [
        (FROM_HUB, [("gateG", "9:00"), ("OAK", "9:25")]),
    ]


@pytest.mark.parametrize(("depart", "joined"), [("9:40", True), ("9:41", False)])
def test_trips_join_when_the_next_leaves_within_20_minutes(depart: str, joined: bool) -> None:
    f = feed(
        ("B1", [("gateG", "9:00", "9:00"), ("ALA", "9:20", "9:20")]),
        ("B1", [("ALA", depart, depart), ("OAK", "10:00", "10:00")]),
    )
    expected = [(FROM_HUB, [("gateG", "9:00"), ("ALA", depart), ("OAK", "10:00")])]
    assert shape(build_legs(f, DAY, "hub", {"OAK"})) == (expected if joined else [])


def test_trip_starting_at_another_stop_starts_a_new_run() -> None:
    f = feed(
        ("B1", [("gateG", "9:00", "9:00"), ("ALA", "9:20", "9:20")]),
        ("B1", [("HB", "9:25", "9:25"), ("OAK", "10:00", "10:00")]),
    )
    assert build_legs(f, DAY, "hub", {"OAK"}) == []


def test_trips_without_a_block_are_not_joined() -> None:
    f = feed(
        ("", [("gateG", "9:00", "9:00"), ("ALA", "9:20", "9:20")]),
        ("", [("ALA", "9:25", "9:25"), ("OAK", "10:00", "10:00")]),
    )
    assert build_legs(f, DAY, "hub", {"OAK"}) == []


def test_times_after_midnight_stay_on_the_service_day() -> None:
    f = feed(("B1", [("gateG", "24:10", "24:10"), ("OAK", "24:40", "24:40")]))
    (leg,) = build_legs(f, DAY, "hub", {"OAK"})
    assert (leg.service_date, leg.stops[-1].arrival) == (DAY, t("24:40"))


@pytest.mark.parametrize(
    "calls",
    [
        [("ALA", "9:00", "9:00"), ("OAK", "9:10", "9:10")],  # never reaches the hub
        [("gateG", "9:00", "9:00"), ("OAK", "9:25", "9:30"), ("gateF", "9:55", "9:55")],
    ],
)
def test_runs_that_do_not_join_hub_and_terminal_give_nothing(calls: list[Call]) -> None:
    assert build_legs(feed(("B1", calls)), DAY, "hub", {"ALA"}) == []


def test_no_legs_on_a_day_without_service() -> None:
    assert build_legs(LOOP, date(2026, 10, 3), "hub", {"OAK"}) == []


def test_leg_visiting_a_stop_twice_raises() -> None:
    calls = [
        ("gateG", "9:00", "9:00"),
        ("OAK", "9:20", "9:20"),
        ("HB", "9:40", "9:40"),
        ("OAK", "10:00", "10:00"),
        ("ALA", "10:10", "10:10"),
    ]
    with pytest.raises(LegError, match=r"2026-09-29 block B1: .*OAK, HB, OAK"):
        build_legs(feed(("B1", calls)), DAY, "hub", {"ALA"})


@pytest.mark.parametrize(
    ("calls", "stop"),
    [
        pytest.param(
            [("gateG", "9:00", "9:00"), ("OAK", "8:50", "8:50"), ("ALA", "9:20", "9:20")],
            "OAK",
            id="arrives before the previous departure",
        ),
        pytest.param(
            [("gateG", "9:00", "9:00"), ("OAK", "9:10", "9:05"), ("ALA", "9:20", "9:20")],
            "OAK",
            id="departs before it arrives",
        ),
        pytest.param(
            [("gateG", "9:00", "9:00"), ("OAK", "9:10", "9:10"), ("ALA", "8:30", "8:30")],
            "ALA",
            id="alights before boarding",
        ),
    ],
)
def test_leg_with_times_going_backwards_raises(calls: list[Call], stop: str) -> None:
    with pytest.raises(LegError, match=rf"2026-09-29 block B1: leg goes back in time at {stop}"):
        build_legs(feed(("B1", calls)), DAY, "hub", {"ALA"})


@pytest.mark.parametrize(
    ("hub", "terminals", "other_hubs", "match"),
    [
        ("hub", set(), set(), "no terminals"),
        ("hub", {"ALA", "nowhere"}, set(), "unknown stop 'nowhere'"),
        ("nohub", {"ALA"}, set(), "unknown stop 'nohub'"),
        ("hub", {"ALA"}, {"nohub"}, "unknown stop 'nohub'"),
        ("hub", {"gateG"}, set(), "'gateG' is part of a hub"),
        ("hub", {"SSF"}, {"SSF"}, "'SSF' is part of a hub"),
    ],
)
def test_bad_configuration_raises(
    hub: str, terminals: set[str], other_hubs: set[str], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        build_legs(LOOP, DAY, hub, terminals, other_hubs=other_hubs)
