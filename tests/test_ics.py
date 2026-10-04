import itertools
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar, Event, Timezone
from icalendar.prop import vGeo

from transit_cal.catalog import StopInfo
from transit_cal.ics import (
    DATA_ENDS,
    PRODID,
    SERVICE_ENDED,
    FeedSpec,
    IcsError,
    Place,
    build_calendar,
    leg_key,
    service_datetime,
    uid,
)
from transit_cal.legs import FROM_HUB, TO_HUB, Leg, LegStop

TZ = ZoneInfo("America/Los_Angeles")
STAMP = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
DAY = date(2026, 9, 29)
STOPS = {
    "7208": StopInfo("Main St Alameda"),
    "7209": StopInfo("Oakland"),
    "72012": StopInfo("SF Ferry Building", gate="Gate G"),
    "72011": StopInfo("SF Ferry Building", gate="Gate F"),
}
SPEC = FeedSpec(
    onestop_id="o-9q9p-sanfranciscobayferry",
    operator_name="SF Bay Ferry",
    route_slug="oakland-alameda",
    route_name="Oakland & Alameda",
    direction=TO_HUB,
    direction_slug="to-san-francisco",
    direction_name="To San Francisco",
    operator_url="https://www.sfbayferry.com/routes-schedules/oakland-alameda/",
    page_url="https://transit-cal.example/o-9q9p-sanfranciscobayferry/oakland-alameda/",
)
FROM_SPEC = replace(SPEC, direction=FROM_HUB, direction_slug="from-san-francisco")
FEED_KEY = "o-9q9p-sanfranciscobayferry/oakland-alameda-to-san-francisco"
PLACES = {
    "7208": Place(
        "Main Street Alameda Ferry Terminal", "2990 Main Street, Alameda", 37.79076, -122.29398
    ),
    "72012": Place(
        "San Francisco Ferry Building Gate G",
        "1 Ferry Building, San Francisco",
        37.79439,
        -122.39093,
    ),
    "7209": Place("Oakland Ferry Terminal", "", None, None),
}


def hm(h: int, m: int, s: int = 0) -> int:
    return h * 3600 + m * 60 + s


def leg(
    *stops: tuple[str, int] | tuple[str, int, int], day: date = DAY, direction: str = TO_HUB
) -> Leg:
    """Stops as (stop, time) or (stop, arrival, departure)."""
    calls = [s if len(s) == 3 else (s[0], s[1], s[1]) for s in stops]
    return Leg(day, direction, tuple(LegStop(*c) for c in calls))


J1 = leg(("7208", hm(10, 50)), ("7209", hm(11, 0), hm(11, 5)), ("72012", hm(11, 30)))
J2 = leg(("7209", hm(5, 55)), ("72012", hm(6, 20)))
FROM_J = leg(("72012", hm(6, 25)), ("7209", hm(7, 0)), direction=FROM_HUB)


def build(
    legs: list[Leg],
    *,
    spec: FeedSpec = SPEC,
    last_departure: date = date(2026, 11, 1),
    stamp: datetime = STAMP,
    places: dict[str, Place] | None = None,
    stops: dict[str, StopInfo] = STOPS,
) -> Calendar:
    return build_calendar(
        spec,
        legs,
        stops=stops,
        places=places or {},
        tz=TZ,
        stamp=stamp,
        last_departure=last_departure,
        window_end=date(2026, 10, 31),
        provenance="511.org feed 54d9f7b6",
    )


def events(cal: Calendar) -> list[Event]:
    return [c for c in cal.walk("VEVENT") if isinstance(c, Event)]


def markers(cal: Calendar) -> list[Event]:
    return [e for e in events(cal) if "No departures scheduled" in str(e["SUMMARY"])]


# Frozen forever: changing any of these changes every subscriber's event UIDs.
@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (f"{FEED_KEY}/20260929/10:50:00/7208", "798fbe23-cba9-5d44-8fe4-ba2fbef9cf10"),
        (f"{FEED_KEY}/20260929/25:10:00/7209", "0bbec138-882d-526c-bf34-5eb299466cb5"),
        (FEED_KEY, "1b95ec43-15ed-5841-8e41-612e58212fc6"),
        (f"{FEED_KEY}#data-ends", "43de54ec-3403-59dd-b03c-7f3e9349b7f4"),
        (f"{FEED_KEY}#service-ended", "bc0375ea-e195-54f2-a439-e98a3079401f"),
    ],
)
def test_uid_golden_vectors(key: str, expected: str) -> None:
    assert uid(key) == expected


def test_special_keys_are_frozen() -> None:
    assert (DATA_ENDS, SERVICE_ENDED) == ("#data-ends", "#service-ended")


def test_feed_key_and_path_follow_the_url_schema() -> None:
    assert SPEC.key == FEED_KEY
    assert SPEC.path == f"{FEED_KEY}.ics"


def test_to_hub_key_ends_with_the_boarding_terminal() -> None:
    assert leg_key(SPEC, J1) == f"{FEED_KEY}/20260929/10:50:00/7208"


def test_from_hub_key_ends_with_the_alighting_terminal() -> None:
    assert leg_key(FROM_SPEC, FROM_J) == (
        "o-9q9p-sanfranciscobayferry/oakland-alameda-from-san-francisco/20260929/06:25:00/7209"
    )


@pytest.mark.parametrize(
    ("departure", "rendered"),
    [(hm(25, 10), "25:10:00"), (hm(7, 30, 5), "07:30:05"), (hm(100, 0), "100:00:00")],
)
def test_key_renders_gtfs_time_with_seconds_and_hours_past_24(
    departure: int, rendered: str
) -> None:
    j = leg(("7209", departure), ("72012", departure + 600))
    assert leg_key(SPEC, j) == f"{FEED_KEY}/20260929/{rendered}/7209"


def test_hub_gate_change_keeps_the_uid() -> None:
    gate_f = leg(("7208", hm(10, 50)), ("7209", hm(11, 0), hm(11, 5)), ("72011", hm(11, 30)))
    (a,), (b,) = events(build([J1])), events(build([gate_f]))
    assert str(a["UID"]) == str(b["UID"])


def test_retimed_leg_gets_a_new_uid() -> None:
    later = leg(("7208", hm(10, 55)), ("7209", hm(11, 5), hm(11, 10)), ("72012", hm(11, 35)))
    (a,), (b,) = events(build([J1])), events(build([later]))
    assert str(a["UID"]) != str(b["UID"])


def test_event_and_calendar_uids_are_bare_lowercase_uuids_of_their_keys() -> None:
    cal = build([J1, J2], last_departure=date(2026, 10, 20))
    assert str(cal["UID"]) == uid(FEED_KEY)
    by_summary = {str(e["SUMMARY"]): str(e["UID"]) for e in events(cal)}
    assert by_summary["Main St Alameda → SF Ferry Building"] == uid(leg_key(SPEC, J1))
    assert by_summary["Oakland → SF Ferry Building"] == uid(leg_key(SPEC, J2))
    (marker,) = markers(cal)
    assert str(marker["UID"]) == uid(FEED_KEY + DATA_ENDS)
    pattern = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
    assert all(pattern.fullmatch(u) for u in [str(cal["UID"]), *by_summary.values()])


def test_identical_legs_collapse_to_one_event() -> None:
    twin = leg(("7209", hm(5, 55)), ("72012", hm(6, 20)))
    assert twin == J2 and twin is not J2
    assert len(events(build([J2, J1, twin]))) == 2


@pytest.mark.parametrize(
    "other",
    [
        pytest.param(leg(("7208", hm(10, 50)), ("72012", hm(11, 30))), id="fewer stops"),
        pytest.param(
            leg(("7208", hm(10, 50)), ("7209", hm(11, 1), hm(11, 5)), ("72012", hm(11, 30))),
            id="intermediate arrival",
        ),
        pytest.param(
            leg(("7208", hm(10, 50)), ("7209", hm(11, 0), hm(11, 6)), ("72012", hm(11, 30))),
            id="intermediate departure",
        ),
        pytest.param(
            leg(("7208", hm(10, 50)), ("7209", hm(11, 0), hm(11, 5)), ("72012", hm(11, 31))),
            id="final arrival",
        ),
        pytest.param(
            leg(("7208", hm(10, 50)), ("7209", hm(11, 0), hm(11, 5)), ("72011", hm(11, 30))),
            id="hub gate",
        ),
    ],
)
def test_same_key_with_different_stops_or_times_raises(other: Leg) -> None:
    with pytest.raises(IcsError) as e:
        build([J1, other])
    message = str(e.value)
    assert SPEC.path in message
    assert "72012" in message
    assert ("72011" if other.stops[-1].stop_id == "72011" else "7208") in message
    assert message.count("2026-09-29") >= 2


def test_leg_in_the_other_direction_raises() -> None:
    with pytest.raises(IcsError, match="from-hub"):
        build([J1, FROM_J])


def test_unknown_spec_direction_raises() -> None:
    sideways = replace(SPEC, direction="sideways")
    with pytest.raises(IcsError, match="sideways"):
        build([], spec=sideways)


def test_stop_without_catalog_info_raises() -> None:
    with pytest.raises(IcsError, match="7208"):
        build([J1], stops={k: v for k, v in STOPS.items() if k != "7208"})


def test_output_bytes_do_not_depend_on_input_order() -> None:
    # Same departure, different terminals: the key breaks the tie.
    to_ala = leg(("7208", hm(5, 55)), ("72012", hm(6, 25)))
    later_day = leg(("7209", hm(5, 0)), ("72012", hm(5, 25)), day=DAY + timedelta(days=1))
    js = [J1, J2, to_ala, later_day]
    outputs = {build(list(p)).to_ical() for p in itertools.permutations(js)}
    assert len(outputs) == 1


@pytest.mark.parametrize(
    "days",
    [
        pytest.param([date(2026, 9, 29), date(2026, 11, 2)], id="across a DST change"),
        pytest.param([date(2040, 1, 10), date(2040, 7, 10)], id="after 2038"),
    ],
)
def test_embedded_timezone_matches_the_real_offset_at_every_event(days: list[date]) -> None:
    legs = [leg(("7208", hm(9, 0)), ("72012", hm(9, 30)), day=d) for d in days]
    cal = Calendar.from_ical(build(legs).to_ical())
    (vtimezone,) = cal.walk("VTIMEZONE")
    assert isinstance(vtimezone, Timezone)
    embedded = vtimezone.to_tz(lookup_tzid=False)
    parsed = events(cal)
    assert len(parsed) == len(days)
    for e in parsed:
        for moment in (e.start, e.end):
            assert isinstance(moment, datetime)
            assert moment.replace(tzinfo=embedded).utcoffset() == moment.utcoffset(), moment


@pytest.mark.parametrize("day", [date.min, date.max], ids=["date.min", "date.max"])
def test_events_at_the_edge_of_the_date_range_raise(day: date) -> None:
    edge = leg(("7208", hm(9, 0)), ("72012", hm(9, 30)), day=day)
    with pytest.raises(IcsError, match=f"{FEED_KEY}.ics: .*{day}.* out of range"):
        build([edge], last_departure=date.max)


def test_naive_stamp_raises() -> None:
    with pytest.raises(IcsError, match="timezone"):
        build([J1], stamp=STAMP.replace(tzinfo=None))


def test_stamp_is_written_in_utc() -> None:
    pacific = datetime(2026, 9, 28, 5, 0, tzinfo=timezone(timedelta(hours=-7)))
    raw = build([J1], stamp=pacific, last_departure=date(2026, 10, 20)).to_ical().decode()
    assert raw.count("DTSTAMP:20260928T120000Z\r\n") == 2


def test_revision_fields_sequence_zero_no_method_no_last_modified() -> None:
    cal = build([J1], last_departure=date(2026, 10, 20))
    assert len(events(cal)) == 2
    assert all(e["SEQUENCE"] == 0 for e in events(cal))
    raw = cal.to_ical().decode()
    assert "METHOD" not in raw
    assert "LAST-MODIFIED" not in raw


@pytest.mark.parametrize(
    ("day", "seconds", "expected"),
    [
        (DAY, hm(5, 55), datetime(2026, 9, 29, 5, 55, tzinfo=TZ)),
        (DAY, hm(24, 5), datetime(2026, 9, 30, 0, 5, tzinfo=TZ)),
        # DST ends 2026-11-01; GTFS day starts at noon minus 12h, 01:00 PDT.
        (date(2026, 11, 1), hm(0, 30), datetime(2026, 11, 1, 1, 30, tzinfo=TZ)),
        (date(2026, 11, 1), hm(6, 0), datetime(2026, 11, 1, 6, 0, tzinfo=TZ)),
        (date(2026, 11, 1), hm(13, 0), datetime(2026, 11, 1, 13, 0, tzinfo=TZ)),
    ],
)
def test_service_datetime_follows_gtfs_noon_minus_twelve(
    day: date, seconds: int, expected: datetime
) -> None:
    got = service_datetime(day, seconds, TZ)
    assert got == expected
    assert got.utcoffset() == expected.utcoffset()


def test_every_event_is_transparent_and_stamped() -> None:
    for e in events(build([J1, J2], last_departure=date(2026, 10, 20))):
        assert str(e["TRANSP"]) == "TRANSPARENT"
        assert e.DTSTAMP == STAMP


def test_event_times_title_and_description() -> None:
    (e,) = events(build([J1]))
    assert e.start == datetime(2026, 9, 29, 10, 50, tzinfo=TZ)
    assert e.end == datetime(2026, 9, 29, 11, 30, tzinfo=TZ)
    assert str(e["SUMMARY"]) == "Main St Alameda → SF Ferry Building"
    stops, footer = str(e["DESCRIPTION"]).split("\n\n")
    assert stops.splitlines() == [
        "Depart Main St Alameda 10:50 AM",
        "Depart Oakland 11:05 AM",
        "Arrive SF Ferry Building (Gate G) 11:30 AM",
    ]
    assert footer.splitlines() == [
        "SF Bay Ferry schedule: https://www.sfbayferry.com/routes-schedules/oakland-alameda/",
        "Schedule details: https://transit-cal.example/o-9q9p-sanfranciscobayferry/oakland-alameda/",
        "Schedule data provided by 511.org (https://511.org) via Transitland",
    ]
    assert str(e["URL"]) == SPEC.operator_url
    assert str(e["LOCATION"]) == "Main St Alameda"


def test_feed_without_a_page_url_omits_the_schedule_details_line() -> None:
    (e,) = events(build([J1], spec=replace(SPEC, page_url=None)))
    footer = str(e["DESCRIPTION"]).split("\n\n")[1].splitlines()
    assert footer == [
        "SF Bay Ferry schedule: https://www.sfbayferry.com/routes-schedules/oakland-alameda/",
        "Schedule data provided by 511.org (https://511.org) via Transitland",
    ]


def test_from_hub_title_reads_from_origin_to_destination() -> None:
    (e,) = events(build([FROM_J], spec=FROM_SPEC))
    assert str(e["SUMMARY"]) == "SF Ferry Building → Oakland"
    assert "Depart SF Ferry Building (Gate G) 6:25 AM" in str(e["DESCRIPTION"])


def test_calendar_properties_serialise_correctly() -> None:
    raw = build([J1]).to_ical().decode()
    assert f"PRODID:{PRODID}\r\n" in raw
    assert PRODID == "-//transit-cal//transit-cal//EN"
    assert "REFRESH-INTERVAL;VALUE=DURATION:P1D\r\n" in raw
    assert "X-PUBLISHED-TTL:P1D\r\n" in raw
    assert "X-WR-CALNAME:SF Bay Ferry · Oakland & Alameda · To San Francisco\r\n" in raw
    assert "X-WR-TIMEZONE:America/Los_Angeles\r\n" in raw
    assert "BEGIN:VTIMEZONE\r\nTZID:America/Los_Angeles" in raw
    assert "DTSTART;TZID=America/Los_Angeles:20260929T105000\r\n" in raw


def test_calendar_description_carries_last_departure_and_provenance() -> None:
    desc = str(build([J1])["DESCRIPTION"])
    assert "Last scheduled departure: 2026-11-01." in desc
    assert "511.org feed 54d9f7b6" in desc
    assert "data provided by 511.org" in desc


def test_last_departure_inside_window_adds_a_marker_the_day_after() -> None:
    (marker,) = markers(build([J1], last_departure=date(2026, 10, 20)))
    assert marker.start == date(2026, 10, 21)
    assert marker.end == date(2026, 10, 22)
    assert str(marker["SUMMARY"]) == "No departures scheduled after Oct 20 for Oakland & Alameda"
    assert str(marker["DESCRIPTION"]) == (
        "The last scheduled departure in the data we have is October 20, 2026. "
        f"Check the operator for newer schedules. {SPEC.operator_url}"
    )


def test_feed_that_ended_before_the_window_keeps_its_marker_date() -> None:
    (marker,) = events(build([], last_departure=date(2026, 9, 3)))
    assert marker.start == date(2026, 9, 4)
    assert str(marker["SUMMARY"]) == "No departures scheduled after Sep 3 for Oakland & Alameda"


@pytest.mark.parametrize("last_departure", [date(2026, 10, 31), date(2026, 11, 1)])
def test_coverage_through_window_end_adds_no_marker(last_departure: date) -> None:
    assert not markers(build([J1], last_departure=last_departure))


def test_empty_leg_list_gives_a_calendar_without_events() -> None:
    raw = build([]).to_ical()
    assert b"BEGIN:VEVENT" not in raw
    assert Calendar.from_ical(raw.decode())["VERSION"] == "2.0"


def test_location_is_boarding_stop_name_and_address() -> None:
    (e,) = events(build([J1], places=PLACES))
    assert str(e["LOCATION"]) == "Main Street Alameda Ferry Terminal\n2990 Main Street, Alameda"


def test_geo_is_boarding_stop_coordinates() -> None:
    (e,) = events(build([J1], places=PLACES))
    geo = e["GEO"]
    assert isinstance(geo, vGeo)
    assert (geo.latitude, geo.longitude) == (37.79076, -122.29398)


def test_apple_structured_location_bytes() -> None:
    raw = build([J1], places=PLACES).to_ical().decode().replace("\r\n ", "")
    line = next(x for x in raw.split("\r\n") if x.startswith("X-APPLE-STRUCTURED-LOCATION"))
    # Mirrors Apple Calendar's own export: parameter order, title only in X-TITLE.
    assert line == (
        "X-APPLE-STRUCTURED-LOCATION;VALUE=URI;"
        'X-ADDRESS="2990 Main Street, Alameda";'
        "X-APPLE-RADIUS=100;X-APPLE-REFERENCEFRAME=1;"
        'X-TITLE="Main Street Alameda Ferry Terminal"'
        ":geo:37.79076,-122.29398"
    )


def test_double_quotes_in_location_parameters_become_single_quotes() -> None:
    places = {"7208": Place('The "Main" Terminal', '1 "A" St', 37.7, -122.2)}
    raw = build([J1], places=places).to_ical().decode().replace("\r\n ", "")
    assert "X-ADDRESS=\"1 'A' St\"" in raw
    assert "X-TITLE=\"The 'Main' Terminal\"" in raw


def test_stop_without_coordinates_gets_text_location_only() -> None:
    (e,) = events(build([J2], places=PLACES))
    assert str(e["LOCATION"]) == "Oakland Ferry Terminal"
    assert "GEO" not in e
    assert "X-APPLE-STRUCTURED-LOCATION" not in e


def unfolded(raw: bytes) -> list[str]:
    return raw.decode().replace("\r\n ", "").split("\r\n")[:-1]


def test_lines_are_crlf_and_folded_to_75_octets() -> None:
    long_name = StopInfo("A very long terminal name " * 6)
    raw = build([J1], stops={**STOPS, "7208": long_name}, places=PLACES).to_ical()
    assert raw.endswith(b"\r\n")
    lines = raw.split(b"\r\n")[:-1]
    assert all(b"\n" not in line and b"\r" not in line for line in lines)
    assert max(len(line) for line in lines) <= 75
    assert any(line.startswith(b" ") for line in lines)


def test_required_properties_are_present() -> None:
    lines = unfolded(build([J1, J2], last_departure=date(2026, 10, 20)).to_ical())
    assert lines[0] == "BEGIN:VCALENDAR"
    assert "VERSION:2.0" in lines
    assert any(x.startswith("PRODID:") for x in lines)
    assert lines.count("BEGIN:VTIMEZONE") == 1
    blocks = "\r\n".join(lines).split("BEGIN:VEVENT")[1:]
    assert len(blocks) == 3
    for block in blocks:
        names = {x.split(":")[0].split(";")[0] for x in block.split("\r\n")}
        assert {"UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY"} <= names
