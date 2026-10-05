"""The synthetic fixture builds the legs and feeds it was designed to produce."""

import zipfile
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar, Event

from transit_cal.build import BuildError, build_feeds
from transit_cal.catalog import load_operators
from transit_cal.gtfs import GtfsError, load
from transit_cal.legs import FROM_HUB, TO_HUB, Leg, build_legs

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"
LA = ZoneInfo("America/Los_Angeles")
STAMP = datetime(2026, 10, 1, 12, tzinfo=LA)
TUESDAY, FRIDAY, SATURDAY = date(2026, 10, 6), date(2026, 10, 9), date(2026, 10, 10)
THANKSGIVING = date(2026, 11, 26)  # weekday service removed, weekend service added
(OPERATOR,) = load_operators(SYNTHETIC / "operators")
ROUTES = {r.slug: r for r in OPERATOR.routes}

Shape = list[tuple[str, list[tuple[str, str]]]]


def hhmm(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}"


def shape(legs: list[Leg]) -> Shape:
    """Each leg as (direction, [(stop, departure)]), ending with the arrival."""
    out = []
    for leg in legs:
        stops = [(s.stop_id, hhmm(s.departure)) for s in leg.stops[:-1]]
        last = leg.stops[-1]
        out.append((leg.direction, [*stops, (last.stop_id, hhmm(last.arrival))]))
    return sorted(out)


def legs_on(gtfs: Path, slug: str, day: date) -> Shape:
    feed = load(gtfs, {OPERATOR.gtfs_agency})[OPERATOR.gtfs_agency]
    route = ROUTES[slug]
    hubs = OPERATOR.other_hubs(route)
    return shape(build_legs(feed, day, route.hub.station, route.terminals, hubs))


@pytest.mark.parametrize(
    ("slug", "day", "want"),
    [
        pytest.param(
            "north",
            TUESDAY,
            [
                # n5 runs through South Station, another route's hub, so it gives no leg.
                (FROM_HUB, [("central-a", "8:00"), ("north", "8:30")]),
                (FROM_HUB, [("central-a", "24:10"), ("north", "24:40")]),
                (TO_HUB, [("north", "8:50"), ("central-b", "9:20")]),
                (TO_HUB, [("north", "24:45"), ("central-a", "25:15")]),
            ],
            id="north weekday",
        ),
        pytest.param(
            "east",
            TUESDAY,
            [
                # e1 and e2 join through a 20-minute wait; e4 and e5 wait 25 and stay apart.
                (FROM_HUB, [("central-a", "10:00"), ("east1", "10:50"), ("east2", "11:00")]),
                (FROM_HUB, [("central-a", "13:00"), ("east1", "13:30")]),
                (TO_HUB, [("east2", "11:20"), ("east1", "11:32"), ("central-b", "12:00")]),
            ],
            id="east weekday",
        ),
        pytest.param(
            "south-line",
            TUESDAY,
            [
                (FROM_HUB, [("south-a", "9:00"), ("east1", "9:30")]),
                (TO_HUB, [("east1", "9:40"), ("south-a", "10:10")]),
            ],
            id="south line from its own hub",
        ),
        pytest.param(
            "north",
            SATURDAY,
            [
                (FROM_HUB, [("central-a", "10:00"), ("north", "10:30")]),
                (TO_HUB, [("north", "11:00"), ("central-a", "11:30")]),
            ],
            id="north weekend",
        ),
        pytest.param(
            "north",
            THANKSGIVING,
            [
                (FROM_HUB, [("central-a", "10:00"), ("north", "10:30")]),
                (TO_HUB, [("north", "11:00"), ("central-a", "11:30")]),
            ],
            id="north on a holiday swap",
        ),
    ],
)
def test_fixture_builds_the_designed_legs(
    synthetic_gtfs: Path, slug: str, day: date, want: Shape
) -> None:
    assert legs_on(synthetic_gtfs, slug, day) == sorted(want)


def test_fixture_builds_every_feed(synthetic_gtfs: Path, tmp_path: Path) -> None:
    results = build_feeds(
        synthetic_gtfs, [OPERATOR], tmp_path / "out", start=TUESDAY, days=7, stamp=STAMP
    )
    assert [r.spec.path for r in results] == [
        f"o-9q9-exampletransit/{route}-{direction}.ics"
        for route, directions in [
            ("north", ("to-central", "from-central")),
            ("east", ("to-central", "from-central")),
            ("south-line", ("to-south", "from-south")),
        ]
        for direction in directions
    ]
    assert all(r.file.is_file() and r.departures > 0 for r in results)


def test_after_midnight_departure_is_dated_the_next_calendar_day(
    synthetic_gtfs: Path, tmp_path: Path
) -> None:
    results = build_feeds(
        synthetic_gtfs, [OPERATOR], tmp_path / "out", start=FRIDAY, days=1, stamp=STAMP
    )
    (north_from,) = [r for r in results if r.spec.path.endswith("north-from-central.ics")]
    cal = Calendar.from_ical(north_from.file.read_bytes())
    starts = sorted(e.start for e in cal.walk("VEVENT") if isinstance(e, Event))
    assert datetime(2026, 10, 10, 0, 10, tzinfo=LA) in starts


def test_packing_is_byte_stable(pack_synthetic: Callable[..., Path], tmp_path: Path) -> None:
    a = pack_synthetic(tmp_path / "a.zip").read_bytes()
    b = pack_synthetic(tmp_path / "b.zip").read_bytes()
    assert a == b


def test_a_dropped_file_is_left_out(pack_synthetic: Callable[..., Path], tmp_path: Path) -> None:
    gtfs = pack_synthetic(tmp_path / "v.zip", {"calendar_dates.txt": None})
    assert "calendar_dates.txt" not in zipfile.ZipFile(gtfs).namelist()


def test_a_replaced_file_reaches_the_loader(
    pack_synthetic: Callable[..., Path], tmp_path: Path
) -> None:
    agency = (
        "agency_id,agency_name,agency_url,agency_timezone\nEX,Example,https://e.test/,Nowhere\n"
    )
    gtfs = pack_synthetic(tmp_path / "v.zip", {"agency.txt": agency})
    with pytest.raises(GtfsError, match="unknown timezone 'Nowhere'"):
        load(gtfs, {"EX"})


def test_an_edited_trip_reaches_the_build(
    pack_synthetic: Callable[..., Path], tmp_path: Path
) -> None:
    backwards = ("n1,north,2,08:30:00,08:30:00", "n1,north,2,07:30:00,07:30:00")
    gtfs = pack_synthetic(tmp_path / "v.zip", {"stop_times.txt": backwards})
    with pytest.raises(BuildError, match="route 'north': .*leg goes back in time at north"):
        build_feeds(gtfs, [OPERATOR], tmp_path / "out", start=TUESDAY, days=1, stamp=STAMP)


def test_an_edit_for_text_that_is_not_there_fails(
    pack_synthetic: Callable[..., Path], tmp_path: Path
) -> None:
    with pytest.raises(AssertionError, match="no such text"):
        pack_synthetic(tmp_path / "v.zip", {"trips.txt": ("no such text", "x")})
