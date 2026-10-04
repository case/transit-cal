"""Legs against operator timetables, and UID keys on the same pinned feeds.

Set TRANSIT_CAL_GTFS to a directory of GTFS zips named `<feed id>-<sha1>.zip`. Each fixture runs
against the zip whose SHA-1 (Transitland's feed version id) it records, and skips when it is absent.
"""

import functools
import hashlib
import json
import os
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from icalendar import Calendar

from transit_cal import cli
from transit_cal.build import build_feeds
from transit_cal.catalog import load_operators
from transit_cal.gtfs import Feed, load
from transit_cal.ics import FeedSpec, leg_key
from transit_cal.legs import FROM_HUB, TO_HUB, Leg, build_legs

FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("*/*/*.json"))
OPERATORS = {op.slug: op for op in load_operators()}
GTFS_DIR = os.environ.get("TRANSIT_CAL_GTFS", "")
# Each feed version is loaded only for the agencies its own fixtures check.
AGENCIES: dict[str, set[str]] = {}
for _path in FIXTURES:
    _doc = json.loads(_path.read_text(encoding="utf-8"))
    AGENCIES.setdefault(_doc["gtfs_feed"], set()).add(OPERATORS[_doc["operator"]].gtfs_agency)

TimetableLeg = tuple[tuple[str, str, str], ...]


@functools.cache
def zip_for(sha1: str) -> Path | None:
    """The zip in TRANSIT_CAL_GTFS whose SHA-1 is sha1, or None when there is no such zip."""
    for path in Path(GTFS_DIR).glob(f"*{sha1}*.zip") if GTFS_DIR else []:
        with path.open("rb") as f:
            if hashlib.file_digest(f, "sha1").hexdigest() == sha1:
                return path
    return None


@functools.cache
def feeds(sha1: str) -> dict[str, Feed] | None:
    """The fixtures' agencies from the zip with this SHA-1, or None when there is no such zip."""
    path = zip_for(sha1)
    return None if path is None else load(path, AGENCIES[sha1])


def timetable_leg(rows: list[list[str]]) -> TimetableLeg:
    return tuple((stop, kind, hhmm) for stop, kind, hhmm in rows)


def project(feed: Feed, leg: Leg, hub: str, kinds: dict[str, str], ends_only: bool) -> TimetableLeg:
    """A leg as a timetable prints it: hub gates as the hub station, times as HH:MM."""
    stops = (leg.stops[0], leg.stops[-1]) if ends_only else leg.stops
    out = []
    for i, s in enumerate(stops):
        stop = hub if feed.stops[s.stop_id].parent_station == hub else s.stop_id
        kind = (
            "depart" if i == 0 else "arrive" if i == len(stops) - 1 else kinds.get(stop, "depart")
        )
        seconds = s.departure if kind == "depart" else s.arrival
        out.append((stop, kind, f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"))
    return tuple(out)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: f"{p.parts[-3]}/{p.parent.name}/{p.stem}")
def test_legs_match_the_operator_timetable(path: Path) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if (by_agency := feeds(doc["gtfs_feed"])) is None:
        pytest.skip(f"no zip for GTFS feed {doc['gtfs_feed']} in TRANSIT_CAL_GTFS={GTFS_DIR!r}")
    op = OPERATORS[doc["operator"]]
    (route,) = [r for r in op.routes if r.slug == doc["route"]]
    feed = by_agency[op.gtfs_agency]
    hub = route.hub.station

    def legs(day: str) -> list[Leg]:
        return build_legs(feed, date.fromisoformat(day), hub, route.terminals, op.other_hubs(route))

    for day in doc["no_service_dates"]:
        assert legs(day) == [], day
    for table in doc["tables"]:
        kinds = {stop: kind for stop, kind in table["columns"]}
        want = Counter(timetable_leg(rows) for rows in table["legs"])
        for day in table["dates"]:
            got = Counter(
                project(feed, j, hub, kinds, doc["ends_only"])
                for j in legs(day)
                if j.direction == table["direction"]
            )
            missing, extra = want - got, got - want
            for diff in table.get("known_differences", []):
                timetable, gtfs_row = timetable_leg(diff["timetable"]), timetable_leg(diff["gtfs"])
                assert (missing[timetable], extra[gtfs_row]) == (1, 1), (table["title"], day)
                missing[timetable] -= 1
                extra[gtfs_row] -= 1
            assert not +missing and not +extra, (table["title"], day, +missing, +extra)


def test_every_route_has_a_timetable_fixture() -> None:
    covered = set()
    for path in FIXTURES:
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["route"] in {r.slug for r in OPERATORS[doc["operator"]].routes}, path
        assert doc["tables"] and all(t["dates"] and t["legs"] for t in doc["tables"]), path
        covered.add((doc["operator"], doc["route"]))
    assert covered == {(op.slug, r.slug) for op in OPERATORS.values() for r in op.routes}


def service_days(feed: Feed) -> list[date]:
    """Every day any service runs, including days added only by calendar_dates."""
    end = feed.service_end_date()
    day = min([p.start for p in feed.patterns.values()] + list(feed.added), default=None)
    days = []
    while day is not None and end is not None and day <= end:
        if feed.active_services(day):
            days.append(day)
        day += timedelta(days=1)
    return days


@pytest.mark.parametrize("sha1", sorted(AGENCIES))
def test_no_two_legs_share_a_uid_key(sha1: str) -> None:
    if (by_agency := feeds(sha1)) is None:
        pytest.skip(f"no zip for GTFS feed {sha1} in TRANSIT_CAL_GTFS={GTFS_DIR!r}")
    checked = 0
    for op in OPERATORS.values():
        if op.gtfs_agency not in by_agency:
            continue
        feed = by_agency[op.gtfs_agency]
        days = service_days(feed)
        assert days, op.slug
        for route in op.routes:
            for direction, d in ((TO_HUB, route.hub.to), (FROM_HUB, route.hub.from_)):
                spec = FeedSpec(op.onestop_id, "", route.slug, "", direction, d.slug, "", "", "")
                seen: dict[str, Leg] = {}
                for day in days:
                    for j in build_legs(
                        feed, day, route.hub.station, route.terminals, op.other_hubs(route)
                    ):
                        if j.direction == direction:
                            key = leg_key(spec, j)
                            assert seen.setdefault(key, j) == j, (key, seen[key], j)
                            checked += 1
    assert checked > 0


@pytest.mark.parametrize("sha1", sorted(AGENCIES))
def test_building_the_pinned_feed_writes_every_published_path(sha1: str, tmp_path: Path) -> None:
    if (path := zip_for(sha1)) is None:
        pytest.skip(f"no zip for GTFS feed {sha1} in TRANSIT_CAL_GTFS={GTFS_DIR!r}")
    start = date(2026, 9, 29)
    stamp = datetime(2026, 9, 28, 12, tzinfo=UTC)
    results = build_feeds(
        path, list(OPERATORS.values()), tmp_path, start=start, days=90, stamp=stamp
    )
    manifest = json.loads((Path(__file__).parent / "published-feeds.json").read_text("utf-8"))
    assert {r.spec.path for r in results} == manifest.keys()
    for r in results:
        cal = Calendar.from_ical(r.file.read_bytes())
        assert cal.walk("VEVENT"), r.spec.path
        assert r.departures > 0 or r.last_departure < start, r.spec.path


@pytest.mark.parametrize("sha1", sorted(AGENCIES))
def test_the_cli_builds_every_published_path_from_the_pinned_feed(
    sha1: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if (path := zip_for(sha1)) is None:
        pytest.skip(f"no zip for GTFS feed {sha1} in TRANSIT_CAL_GTFS={GTFS_DIR!r}")
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1790000000")
    out = tmp_path / "out"
    argv = ["build", str(path), "--out", str(out), "--start", "2026-09-29", "--days", "90"]
    assert cli.main(argv) == 0
    manifest = json.loads((Path(__file__).parent / "published-feeds.json").read_text("utf-8"))
    assert {str(p.relative_to(out)) for p in out.rglob("*.ics")} == manifest.keys()
    assert capsys.readouterr().out.endswith(f"wrote {len(manifest)} feeds under {out}\n")
    assert "DTSTAMP:20260921T" in next(out.rglob("*.ics")).read_text(encoding="utf-8")
