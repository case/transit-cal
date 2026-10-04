import zipfile
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from transit_cal.gtfs import GtfsError, Stop, load, parse_time

GtfsZip = Callable[..., Path]


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("05:55:00", 21300), ("5:55:00", 21300), ("24:05:00", 86700), ("00:00:00", 0)],
)
def test_parse_time_accepts_hours_past_midnight(text: str, seconds: int) -> None:
    assert parse_time(text) == seconds


@pytest.mark.parametrize("text", ["", "5:55", "aa:bb:cc", "05:60:00", "-1:00:00"])
def test_parse_time_rejects_malformed(text: str) -> None:
    with pytest.raises(GtfsError, match="time"):
        parse_time(text)


def test_load_splits_agencies_that_share_services_and_stops(gtfs_zip: GtfsZip) -> None:
    feeds = load(gtfs_zip(), {"A", "B"})
    a, b = feeds["A"], feeds["B"]
    assert (a.routes, set(a.trips)) == ({"A1"}, {"a1", "a2"})
    assert (b.routes, set(b.trips)) == ({"B1"}, {"b1", "b2"})
    assert a.patterns["WK"] == b.patterns["WK"]
    assert "oak" in a.stops and "oak" in b.stops
    assert a.timezone == "America/Los_Angeles"


def test_rows_of_unrequested_agencies_are_not_parsed(gtfs_zip: GtfsZip) -> None:
    feed = load(gtfs_zip(), {"A"})["A"]
    assert "xs" not in feed.stops
    assert "XS" not in feed.patterns


def test_stop_times_are_ordered_by_sequence(gtfs_zip: GtfsZip) -> None:
    feed = load(gtfs_zip(), {"A"})["A"]
    assert [st.stop_id for st in feed.stop_times["a1"]] == ["oak", "gate"]
    assert feed.stop_times["a2"][1].arrival == 24 * 3600 + 40 * 60


def test_stops_keep_their_parent_station_and_location(gtfs_zip: GtfsZip) -> None:
    feeds = load(gtfs_zip(), {"A", "B"})
    assert feeds["A"].stops["hub"].name == "Ferry Terminal"
    assert feeds["A"].stops["oak"] == Stop(
        "oak", "Oakland", "", "1 Main St, Oakland", 37.79509, -122.27976
    )
    assert (feeds["B"].stops["far"].lat, feeds["B"].stops["far"].lon) == (None, None)


def test_load_reads_files_with_a_byte_order_mark(gtfs_zip: GtfsZip) -> None:
    assert set(load(gtfs_zip(bom=True), {"A"})["A"].trips) == {"a1", "a2"}


@pytest.mark.parametrize(
    ("agency", "day", "expected"),
    [
        ("A", date(2026, 9, 29), {"WK"}),  # Tuesday
        ("A", date(2026, 10, 3), set()),  # Saturday: A has no weekend service
        ("A", date(2026, 9, 8), {"SP"}),  # weekday removed, special added
        ("A", date(2026, 6, 29), {"WK"}),  # first day is inclusive
        ("B", date(2026, 11, 1), {"WE"}),  # last day is inclusive
        ("A", date(2026, 11, 2), set()),  # after the calendar ends
    ],
)
def test_active_services_apply_calendar_and_exceptions(
    gtfs_zip: GtfsZip, agency: str, day: date, expected: set[str]
) -> None:
    assert load(gtfs_zip(), {agency})[agency].active_services(day) == expected


def test_feed_with_only_calendar_dates(gtfs_zip: GtfsZip) -> None:
    dates = "service_id,date,exception_type\nWK,20260929,1\n"
    feed = load(gtfs_zip({"calendar.txt": None, "calendar_dates.txt": dates}), {"A"})["A"]
    assert feed.active_services(date(2026, 9, 29)) == {"WK"}
    assert feed.active_services(date(2026, 9, 30)) == set()
    assert feed.service_end_date() == date(2026, 9, 29)


def test_service_end_date_is_the_last_day_any_service_runs(gtfs_zip: GtfsZip) -> None:
    assert load(gtfs_zip(), {"A"})["A"].service_end_date() == date(2026, 11, 1)


@pytest.mark.parametrize(
    ("edits", "match"),
    [
        ({"trips.txt": None}, r"trips\.txt"),
        ({"routes.txt": ("A1,A,4", "A1,X,4")}, "agency 'A' has no routes"),
        ({"stops.txt": ("oak,Oakland,,", "oakland,Oakland,,")}, "stop 'oak'"),
        ({"stops.txt": ("37.79509", "north")}, "coordinate"),
        ({"stops.txt": ("37.79509", "91")}, "coordinate"),
        ({"stops.txt": ("37.79509", "nan")}, "coordinate"),
        ({"stop_times.txt": ("a1,oak,1,", "a1,oak,first,")}, "stop_sequence"),
        ({"calendar.txt": ("WK,1,1,1,1,1,0,0", "WK,1,1,true,1,1,0,0")}, "wednesday"),
        ({"calendar.txt": ("20260629,20261101\nWE", "20261101,20260629\nWE")}, "ends before"),
        ({"calendar_dates.txt": ("WK,20260908,2", "WK,20260908,3")}, "exception_type"),
        ({"calendar_dates.txt": ("WK,20260908", "WK,2026-09-08")}, "date"),
        ({"calendar_dates.txt": ("WK,20260908,2", "WK,20260908")}, "exception_type"),
        ({"stop_times.txt": (",departure_time", ",departure")}, "departure_time"),
        ({"agency.txt": ("America/Los_Angeles\nB", "Mars/Olympus\nB")}, "timezone 'Mars/Olympus'"),
        ({"agency.txt": ("America/Los_Angeles\nB", "\nB")}, "timezone ''"),
        ({"agency.txt": ("America/Los_Angeles\nB", "../zoneinfo/UTC\nB")}, "timezone"),
    ],
)
def test_load_rejects_bad_data_for_requested_agencies(
    gtfs_zip: GtfsZip, edits: dict[str, object], match: str
) -> None:
    with pytest.raises(GtfsError, match=match):
        load(gtfs_zip(edits), {"A"})


def test_load_rejects_an_agency_not_in_the_feed(gtfs_zip: GtfsZip) -> None:
    with pytest.raises(GtfsError, match="agency 'Z' not in feed"):
        load(gtfs_zip(), {"A", "Z"})


def test_load_ignores_the_timezone_of_an_unrequested_agency(gtfs_zip: GtfsZip) -> None:
    path = gtfs_zip({"agency.txt": ("x.test/,America/Los_Angeles", "x.test/,Mars/Olympus")})
    assert load(path, {"A"})["A"].timezone == "America/Los_Angeles"


def test_load_rejects_a_file_that_is_not_a_zip(tmp_path: Path) -> None:
    path = tmp_path / "gtfs.zip"
    path.write_bytes(b"agency_id,agency_name\n")
    with pytest.raises(GtfsError, match="gtfs.zip is not a readable zip"):
        load(path, {"A"})


def test_load_rejects_text_that_is_not_utf8(gtfs_zip: GtfsZip) -> None:
    path = gtfs_zip({"stops.txt": None})
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("stops.txt", "stop_id,stop_name\nhub,Caf\u00e9\n".encode("latin-1"))
    with pytest.raises(GtfsError, match="stops.txt is not UTF-8"):
        load(path, {"A"})
