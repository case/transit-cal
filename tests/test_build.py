import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar, Event
from icalendar.prop import vGeo

from transit_cal import build as build_module
from transit_cal.build import BuildError, FeedResult, build_feeds
from transit_cal.catalog import Operator, load_operators
from transit_cal.ics import DATA_ENDS, uid

STAMP = datetime(2026, 9, 28, 12, tzinfo=UTC)
# 23:30 on Monday 2026-10-05 in Los Angeles, already Tuesday in UTC.
NOW = datetime(2026, 10, 6, 6, 30, tzinfo=UTC)
LA = ZoneInfo("America/Los_Angeles")
TO = "o-9q9-agencya/oak-to-hub.ics"
FROM = "o-9q9-agencya/oak-from-hub.ics"
OPERATOR = """\
name = "Agency A Ferry"
gtfs_agency = "A"
onestop_id = "o-9q9-agencya"
website = "https://a.test/"

[hub]
station = "hub"
to = { slug = "to-hub", name = "To Hub" }
from = { slug = "from-hub", name = "From Hub" }

[[routes]]
slug = "oak"
name = "Oakland"
gtfs_route = "A1"
url = "https://a.test/oak/"
terminals = ["oak"]

[stops.hub]
name = "Ferry Terminal"

[stops.gate]
name = "Ferry Terminal"
gate = "Gate A"

[stops.oak]
name = "Oakland"

[stops.far]
name = "Far Point"
"""
# WK runs weekdays and ends on a Sunday; LATE is a later weekday service.
CALENDAR = """\
service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
WK,1,1,1,1,1,0,0,20260629,20261101
LATE,1,1,1,1,1,0,0,20260629,20261120
"""
Call = tuple[str, str]  # stop, time (arrival and departure)
Trip = tuple[str, str, list[Call]]  # trip_id, service_id, calls
BASIC: list[Trip] = [
    ("in", "WK", [("oak", "05:55:00"), ("gate", "06:20:00")]),
    ("out", "WK", [("gate", "07:00:00"), ("oak", "07:25:00")]),
]


@pytest.fixture
def make_zip(gtfs_zip: Callable[..., Path]) -> Callable[..., Path]:
    """A zip with agency A's trips replaced by the given ones."""

    def write(
        trips: list[Trip],
        calendar: str | None = CALENDAR,
        calendar_dates: str | None = "service_id,date,exception_type\n",
        b_trips: list[Trip] | None = None,
        agency: str | None = None,
    ) -> Path:
        routed = [("A1", t) for t in trips] + [("B1", t) for t in b_trips or []]
        trips_txt = "route_id,service_id,trip_id,block_id\n" + "".join(
            f"{route},{service},{trip_id},\n" for route, (trip_id, service, _) in routed
        )
        stop_times = "trip_id,stop_id,stop_sequence,arrival_time,departure_time\n" + "".join(
            f"{trip_id},{stop},{n},{t},{t}\n"
            for _, (trip_id, _, calls) in routed
            for n, (stop, t) in enumerate(calls, 1)
        )
        return gtfs_zip(
            {
                "trips.txt": trips_txt,
                "stop_times.txt": stop_times,
                "calendar.txt": calendar,
                "calendar_dates.txt": calendar_dates,
                **({"agency.txt": agency} if agency is not None else {}),
            }
        )

    return write


@pytest.fixture
def operators(tmp_path: Path) -> Callable[..., list[Operator]]:
    def load(text: str = OPERATOR) -> list[Operator]:
        directory = tmp_path / "operators"
        directory.mkdir(exist_ok=True)
        (directory / "agency-a.toml").write_text(text, encoding="utf-8")
        return load_operators(directory)

    return load


def run(
    zip_path: Path,
    ops: list[Operator],
    out: Path,
    *,
    start: date = date(2026, 9, 28),
    days: int = 60,
    stamp: datetime = STAMP,
) -> dict[str, FeedResult]:
    return {
        r.spec.path: r for r in build_feeds(zip_path, ops, out, start=start, days=days, stamp=stamp)
    }


def events(path: Path) -> list[Event]:
    cal = Calendar.from_ical(path.read_bytes())
    return [c for c in cal.walk("VEVENT") if isinstance(c, Event)]


def departures(path: Path) -> list[Event]:
    return [e for e in events(path) if "No departures scheduled" not in str(e["SUMMARY"])]


def markers(path: Path) -> list[Event]:
    return [e for e in events(path) if "No departures scheduled" in str(e["SUMMARY"])]


def last_day(path: Path) -> date:
    starts = [e.start for e in departures(path)]
    return max(s.date() if isinstance(s, datetime) else s for s in starts)


def tree(out: Path) -> dict[str, bytes]:
    return {str(p.relative_to(out)): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}


def test_writes_one_feed_per_direction_at_its_permanent_path(make_zip, operators, tmp_path) -> None:
    out = tmp_path / "out"
    results = run(make_zip(BASIC), operators(), out)
    assert list(results) == [TO, FROM]
    assert sorted(tree(out)) == sorted([TO, FROM])
    for path, r in results.items():
        assert r.file == out / path
        assert len(departures(r.file)) == r.departures
        assert r.start == date(2026, 9, 28)
    # 2026-09-28..2026-10-30 holds 25 weekdays; WK ends on Sunday 2026-11-01.
    assert results[TO].departures == results[FROM].departures == 25


def test_last_departure_is_per_direction_and_ignores_trips_that_miss_the_hub(
    make_zip, operators, tmp_path
) -> None:
    trips = [
        *BASIC,
        ("late_out", "LATE", [("gate", "08:00:00"), ("oak", "08:25:00")]),
        ("no_hub", "XMAS", [("oak", "09:00:00"), ("far", "09:30:00")]),
    ]
    calendar = CALENDAR + "XMAS,1,1,1,1,1,1,1,20260629,20261231\n"
    results = run(make_zip(trips, calendar), operators(), tmp_path / "out")
    assert results[TO].last_departure == date(2026, 10, 30)  # the Friday before WK's Sunday end
    assert results[FROM].last_departure == date(2026, 11, 20)


def test_last_departure_uses_the_local_date_of_a_departure_after_midnight(
    make_zip, operators, tmp_path
) -> None:
    trips = [*BASIC, ("owl", "OWL", [("oak", "24:05:00"), ("gate", "24:40:00")])]
    calendar = CALENDAR + "OWL,0,0,0,0,0,0,0,20261102,20261102\n"
    dates = "service_id,date,exception_type\nOWL,20261102,1\n"
    results = run(make_zip(trips, calendar, dates), operators(), tmp_path / "out")
    r = results[TO]
    assert r.last_departure == date(2026, 11, 3)
    (marker,) = markers(r.file)
    assert marker.start == date(2026, 11, 4)
    cal = Calendar.from_ical(r.file.read_bytes())
    assert "Last scheduled departure: 2026-11-03." in str(cal["DESCRIPTION"])


def test_window_is_clipped_at_the_last_departure_and_marked(make_zip, operators, tmp_path) -> None:
    results = run(make_zip(BASIC), operators(), tmp_path / "out", start=date(2026, 10, 26), days=14)
    r = results[TO]
    assert r.departures == 5
    assert last_day(r.file) == date(2026, 10, 30)
    (marker,) = markers(r.file)
    assert marker.start == date(2026, 10, 31)


def test_window_inside_the_data_has_no_marker(make_zip, operators, tmp_path) -> None:
    results = run(make_zip(BASIC), operators(), tmp_path / "out", start=date(2026, 10, 1), days=10)
    assert not markers(results[TO].file)
    # 2026-10-01..2026-10-10 holds 7 weekdays; later departures are outside the window.
    assert results[TO].departures == 7
    assert last_day(results[TO].file) == date(2026, 10, 9)


def test_feed_that_ended_before_the_window_is_written_with_only_its_marker(
    make_zip, operators, tmp_path
) -> None:
    results = run(make_zip(BASIC), operators(), tmp_path / "out", start=date(2026, 11, 23))
    r = results[TO]
    assert (r.departures, departures(r.file)) == (0, [])
    (marker,) = markers(r.file)
    assert marker.start == date(2026, 10, 31)


def test_marker_date_and_uid_do_not_move_with_the_window(make_zip, operators, tmp_path) -> None:
    zip_path, ops = make_zip(BASIC), operators()
    a = markers(run(zip_path, ops, tmp_path / "a", start=date(2026, 11, 23))[TO].file)
    b = markers(run(zip_path, ops, tmp_path / "b", start=date(2026, 11, 30))[TO].file)
    assert [(m.start, str(m["UID"])) for m in a] == [(m.start, str(m["UID"])) for m in b]
    assert str(a[0]["UID"]) == uid(TO.removesuffix(".ics") + DATA_ENDS)


@pytest.mark.parametrize(
    ("trips", "calendar", "dates"),
    [
        pytest.param(BASIC[:1], CALENDAR, "service_id,date,exception_type\n", id="one direction"),
        pytest.param(BASIC, None, None, id="no calendar files"),
        pytest.param(
            [(t, "ZZ", calls) for t, _, calls in BASIC], CALENDAR, None, id="uncalendared service"
        ),
    ],
)
def test_a_feed_with_no_departures_anywhere_raises_and_writes_nothing(
    make_zip, operators, tmp_path, trips, calendar, dates
) -> None:
    out = tmp_path / "out"
    with pytest.raises(BuildError, match="no departures"):
        run(make_zip(trips, calendar, dates), operators(), out)
    assert not out.exists() or tree(out) == {}


def test_a_failing_feed_leaves_every_existing_file_untouched(make_zip, operators, tmp_path) -> None:
    out = tmp_path / "out"
    run(make_zip(BASIC), operators(), out)
    before = tree(out)
    # A second route whose terminal has no departures fails after the first route is generated.
    broken = OPERATOR.replace(
        "[stops.hub]",
        '[[routes]]\nslug = "far"\nname = "Far"\ngtfs_route = "A1"\nurl = "https://a.test/far/"\n'
        'terminals = ["far"]\n\n[stops.hub]',
    )
    trips = [*BASIC, ("no_hub", "WK", [("oak", "09:00:00"), ("far", "09:30:00")])]
    with pytest.raises(BuildError, match="oak-|far-"):
        run(make_zip(trips), operators(broken), out, start=date(2026, 10, 5))
    assert tree(out) == before


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"onestop_id": "o-9q9-other"}, "gtfs_agency 'A'"),
        ({"gtfs_agency": "B"}, "onestop_id 'o-9q9-agencya'"),
    ],
)
def test_two_operators_sharing_an_identity_raise(
    make_zip, operators, tmp_path, change, match
) -> None:
    (op,) = operators()
    other = replace(op, slug="other", routes=tuple(replace(r, slug="other") for r in op.routes))
    with pytest.raises(BuildError, match=match):
        run(make_zip(BASIC), [op, replace(other, **change)], tmp_path / "out")


def test_a_route_listed_twice_raises_before_writing(make_zip, operators, tmp_path) -> None:
    (op,) = operators()
    with pytest.raises(BuildError, match=f"feed path '{FROM}' built twice"):
        run(make_zip(BASIC), [replace(op, routes=op.routes * 2)], tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_results_follow_operator_slugs_whatever_the_input_order(
    make_zip, operators, tmp_path
) -> None:
    (a,) = operators()
    b = replace(
        a,
        slug="agency-b",
        gtfs_agency="B",
        onestop_id="o-9q9-agencyb",
        routes=tuple(replace(r, gtfs_route="B1") for r in a.routes),
    )
    b_trips = [(f"b_{t}", service, calls) for t, service, calls in BASIC]
    results = run(make_zip(BASIC, b_trips=b_trips), [b, a], tmp_path / "out")
    assert [r.spec.onestop_id for r in results.values()] == ["o-9q9-agencya"] * 2 + [
        "o-9q9-agencyb"
    ] * 2


def test_a_zip_replaced_while_it_is_read_raises(make_zip, operators, tmp_path, monkeypatch) -> None:
    zip_path = make_zip(BASIC)
    real_load = build_module.load

    def load_then_replace(path: Path, agencies: set[str]):
        feeds = real_load(path, agencies)
        path.write_bytes(path.read_bytes() + b"\0")
        return feeds

    monkeypatch.setattr(build_module, "load", load_then_replace)
    with pytest.raises(BuildError, match="changed while it was read"):
        run(zip_path, operators(), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_route_missing_from_the_gtfs_raises(make_zip, operators, tmp_path) -> None:
    ops = operators(OPERATOR.replace('gtfs_route = "A1"', 'gtfs_route = "A9"'))
    with pytest.raises(BuildError, match="A9"):
        run(make_zip(BASIC), ops, tmp_path / "out")


def test_an_operator_given_twice_raises(make_zip, operators, tmp_path) -> None:
    ops = operators()
    with pytest.raises(BuildError, match="o-9q9-agencya"):
        run(make_zip(BASIC), ops * 2, tmp_path / "out")


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"days": 0}, "days"),
        ({"stamp": STAMP.replace(tzinfo=None)}, "timezone"),
    ],
)
def test_bad_arguments_raise(make_zip, operators, tmp_path, kwargs, match) -> None:
    with pytest.raises(BuildError, match=match):
        run(make_zip(BASIC), operators(), tmp_path / "out", **kwargs)


def test_no_operators_raises(make_zip, tmp_path) -> None:
    with pytest.raises(BuildError, match="no operators"):
        run(make_zip(BASIC), [], tmp_path / "out")


@pytest.mark.parametrize(("span", "ok"), [(1000, True), (1001, False)])
def test_service_range_is_capped_at_1000_days(make_zip, operators, tmp_path, span, ok) -> None:
    end = date(2026, 6, 29) + timedelta(days=span - 1)
    calendar = CALENDAR.replace("20261120", f"{end:%Y%m%d}")
    late = ("late_out", "LATE", [("gate", "08:00:00"), ("oak", "08:25:00")])
    zip_path = make_zip([*BASIC, late], calendar)
    if ok:
        assert run(zip_path, operators(), tmp_path / "out")[FROM].last_departure > end - timedelta(
            7
        )
    else:
        with pytest.raises(BuildError, match="1001 days"):
            run(zip_path, operators(), tmp_path / "out")


def test_places_fall_back_to_the_gtfs_stop(make_zip, operators, tmp_path) -> None:
    r = run(make_zip(BASIC), operators(), tmp_path / "out")[TO]
    event = departures(r.file)[0]
    assert str(event["LOCATION"]) == "Oakland\n1 Main St, Oakland"
    geo = event["GEO"]
    assert isinstance(geo, vGeo)
    assert (geo.latitude, geo.longitude) == (37.79509, -122.27976)


def test_catalog_place_title_and_address_win_over_the_gtfs(make_zip, operators, tmp_path) -> None:
    text = OPERATOR.replace(
        '[stops.oak]\nname = "Oakland"',
        '[stops.oak]\nname = "Oakland"\nplace = "Oakland Ferry Terminal"\naddress = "2 Dock Rd"',
    )
    r = run(make_zip(BASIC), operators(text), tmp_path / "out")[TO]
    assert str(departures(r.file)[0]["LOCATION"]) == "Oakland Ferry Terminal\n2 Dock Rd"


def test_descriptions_have_no_schedule_details_link(make_zip, operators, tmp_path) -> None:
    r = run(make_zip(BASIC), operators(), tmp_path / "out")[TO]
    assert "Schedule details" not in r.file.read_text(encoding="utf-8")


def test_provenance_names_the_zip_by_its_computed_sha1(make_zip, operators, tmp_path) -> None:
    zip_path = make_zip(BASIC)
    renamed = zip_path.rename(tmp_path / "f-feed-0000000000000000000000000000000000000000.zip")
    sha = hashlib.sha1(renamed.read_bytes(), usedforsecurity=False).hexdigest()
    r = run(renamed, operators(), tmp_path / "out")[TO]
    desc = str(Calendar.from_ical(r.file.read_bytes())["DESCRIPTION"])
    assert f"Transitland feed version {sha[:8]}, built 2026-09-28" in desc


def test_same_inputs_give_identical_files(make_zip, operators, tmp_path) -> None:
    zip_path, ops = make_zip(BASIC), operators()
    run(zip_path, ops, tmp_path / "a")
    run(zip_path, ops, tmp_path / "b")
    assert tree(tmp_path / "a") == tree(tmp_path / "b")


def test_rebuild_replaces_files_world_readable_with_no_temp_left(
    make_zip, operators, tmp_path
) -> None:
    out = tmp_path / "out"
    (out / TO).parent.mkdir(parents=True)
    (out / TO).write_bytes(b"old")
    run(make_zip(BASIC), operators(), out)
    assert (out / TO).read_bytes().startswith(b"BEGIN:VCALENDAR")
    assert stat.S_IMODE((out / TO).stat().st_mode) == 0o644
    assert sorted(tree(out)) == sorted([TO, FROM])


@pytest.mark.parametrize("failing", ["fsync", "replace"])
def test_a_failed_write_keeps_the_old_file_and_removes_the_temp(
    make_zip, operators, tmp_path, monkeypatch, failing
) -> None:
    out = tmp_path / "out"
    (out / TO).parent.mkdir(parents=True)
    (out / TO).write_bytes(b"old")
    replaced: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def replace(src: str, dst: str) -> None:
        replaced.append((Path(src), Path(dst)))
        if failing == "replace":
            raise OSError("disk full")
        real_replace(src, dst)

    def fsync(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(build_module.os, "replace", replace)
    if failing == "fsync":
        monkeypatch.setattr(build_module.os, "fsync", fsync)
    with pytest.raises(OSError, match="disk full"):
        run(make_zip(BASIC), operators(), out)
    assert (out / TO).read_bytes() == b"old"
    assert tree(out) == {TO: b"old"}
    assert all(src.parent == dst.parent for src, dst in replaced)


def test_default_start_is_yesterday_in_the_agency_timezone(make_zip, operators, tmp_path) -> None:
    out = tmp_path / "out"
    results = build_feeds(make_zip(BASIC), operators(), out, days=3, stamp=STAMP, now=NOW)
    assert [r.start for r in results] == [date(2026, 10, 4)] * 2
    # Sunday 10-04 to Tuesday 10-06 holds two weekdays; a UTC start (10-05) would hold three.
    assert [r.departures for r in results] == [2, 2]


def test_default_start_keeps_last_nights_departures_after_midnight(
    make_zip, operators, tmp_path
) -> None:
    trips = [*BASIC, ("owl", "OWL", [("oak", "24:05:00"), ("gate", "24:40:00")])]
    calendar = CALENDAR + "OWL,0,0,0,0,0,0,0,20261005,20261005\n"
    dates = "service_id,date,exception_type\nOWL,20261005,1\n"
    now = datetime(2026, 10, 6, 7, 1, tzinfo=UTC)  # 00:01 on 2026-10-06 in Los Angeles
    zip_path = make_zip(trips, calendar, dates)
    to, _ = build_feeds(zip_path, operators(), tmp_path / "out", days=1, stamp=STAMP, now=now)
    assert datetime(2026, 10, 6, 0, 5, tzinfo=LA) in [e.start for e in departures(to.file)]


def test_default_start_follows_each_agency_timezone(make_zip, operators, tmp_path) -> None:
    (a,) = operators()
    b = replace(
        a,
        slug="agency-b",
        gtfs_agency="B",
        onestop_id="o-dr5-agencyb",
        routes=tuple(replace(r, gtfs_route="B1") for r in a.routes),
    )
    agency = (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "A,Agency A,https://a.test/,America/Los_Angeles\n"
        "B,Agency B,https://b.test/,America/New_York\n"
    )
    b_trips = [(f"b_{t}", service, calls) for t, service, calls in BASIC]
    zip_path = make_zip(BASIC, b_trips=b_trips, agency=agency)
    now = datetime(2026, 10, 6, 4, 30, tzinfo=UTC)  # 21:30 on 10-05 in LA, 00:30 on 10-06 in NY
    results = build_feeds(zip_path, [a, b], tmp_path / "out", days=3, stamp=STAMP, now=now)
    assert [(r.spec.onestop_id, r.start) for r in results[::2]] == [
        ("o-9q9-agencya", date(2026, 10, 4)),
        ("o-dr5-agencyb", date(2026, 10, 5)),
    ]


def test_an_explicit_start_ignores_now(make_zip, operators, tmp_path) -> None:
    results = build_feeds(
        make_zip(BASIC),
        operators(),
        tmp_path / "out",
        start=date(2026, 9, 28),
        days=3,
        stamp=STAMP,
        now=NOW,
    )
    assert {r.start for r in results} == {date(2026, 9, 28)}


@pytest.mark.parametrize("now", [None, NOW.replace(tzinfo=None)], ids=["missing", "naive"])
def test_default_start_needs_now_with_a_timezone(make_zip, operators, tmp_path, now) -> None:
    with pytest.raises(BuildError, match="now"):
        build_feeds(make_zip(BASIC), operators(), tmp_path / "out", days=3, stamp=STAMP, now=now)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("start", "days"),
    [(date(2026, 9, 28), 10**9), (date(2026, 9, 28), 10**10), (date.max, 2)],
    ids=["past date.max", "past timedelta", "from date.max"],
)
def test_a_window_past_the_last_representable_date_raises(
    make_zip, operators, tmp_path, start, days
) -> None:
    with pytest.raises(BuildError, match="window"):
        run(make_zip(BASIC), operators(), tmp_path / "out", start=start, days=days)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("terminals", "match"),
    [('["far"]', "unknown stop 'far'"), ('["gate"]', "terminal 'gate' is part of a hub")],
)
def test_a_route_stop_the_gtfs_cannot_place_raises(
    make_zip, operators, tmp_path, terminals, match
) -> None:
    ops = operators(OPERATOR.replace('terminals = ["oak"]', f"terminals = {terminals}"))
    with pytest.raises(BuildError, match=f"agency-a: route 'oak': {match}"):
        run(make_zip(BASIC), ops, tmp_path / "out")


def test_an_unknown_route_stop_raises_even_without_service_days(
    make_zip, operators, tmp_path
) -> None:
    ops = operators(OPERATOR.replace('terminals = ["oak"]', 'terminals = ["far"]'))
    with pytest.raises(BuildError, match="agency-a: route 'oak': unknown stop 'far'"):
        run(make_zip(BASIC, calendar=None, calendar_dates=None), ops, tmp_path / "out")


def test_a_clock_at_the_first_representable_date_raises(make_zip, operators, tmp_path) -> None:
    now = datetime.min.replace(tzinfo=UTC)
    with pytest.raises(BuildError, match="out of date range"):
        build_feeds(make_zip(BASIC), operators(), tmp_path / "out", days=3, stamp=STAMP, now=now)
