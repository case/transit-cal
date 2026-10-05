from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from transit_cal import cli
from transit_cal.build import BuildError, FeedResult
from transit_cal.catalog import load_operators

NOW = datetime(2026, 10, 6, 6, 30, tzinfo=UTC)
SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"
# Agency A from conftest's GTFS, with one trip added from the hub so both directions run.
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
"""
FROM_HUB_TRIP = {
    "trips.txt": ("A1,SP,a2,\n", "A1,SP,a2,\nA1,WK,a3,\n"),
    "stop_times.txt": (
        "b1,oak,1,",
        "a3,gate,1,07:00:00,07:00:00\na3,oak,2,07:25:00,07:25:00\nb1,oak,1,",
    ),
}


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace build_feeds with a recorder that writes nothing and returns no feeds."""
    recorded: list[dict[str, Any]] = []

    def record(gtfs: Path, operators: list[Any], out: Path, **kwargs: Any) -> list[FeedResult]:
        recorded.append({"gtfs": gtfs, "out": out, **kwargs})
        return []

    monkeypatch.setattr(cli, "build_feeds", record)
    return recorded


@pytest.fixture
def agency_a(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "operators"
    directory.mkdir()
    (directory / "agency-a.toml").write_text(OPERATOR, encoding="utf-8")
    monkeypatch.setattr(cli, "load_operators", lambda _=None: load_operators(directory))


def test_defaults_build_60_days_from_the_agency_clock_into_out(calls) -> None:
    assert cli.main(["build", "feed.zip"]) == 0
    assert calls == [
        {
            "gtfs": Path("feed.zip"),
            "out": Path("out"),
            "start": None,
            "days": 60,
            "stamp": NOW,
            "now": NOW,
        }
    ]


def test_options_reach_the_build(calls) -> None:
    argv = ["build", "feed.zip", "--out", "site", "--start", "2026-11-01", "--days", "7"]
    assert cli.main(argv) == 0
    (call,) = calls
    assert (call["out"], call["start"], call["days"]) == (Path("site"), date(2026, 11, 1), 7)


@pytest.mark.parametrize(
    ("value", "stamp"),
    [
        ("0", datetime(1970, 1, 1, tzinfo=UTC)),
        ("1759600000", datetime(2025, 10, 4, 17, 46, 40, tzinfo=UTC)),
    ],
)
def test_source_date_epoch_sets_the_stamp_but_not_the_clock(
    calls, monkeypatch, value, stamp
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", value)
    assert cli.main(["build", "feed.zip"]) == 0
    (call,) = calls
    assert (call["stamp"], call["now"]) == (stamp, NOW)


@pytest.mark.parametrize(
    "value",
    ["", " 12", "12 ", "+12", "-1", "1.5", "1_000", "1e9", "١٢", "9" * 30],
)
def test_malformed_source_date_epoch_fails(calls, monkeypatch, capsys, value) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", value)
    assert cli.main(["build", "feed.zip"]) == 1
    assert calls == []
    err = capsys.readouterr().err
    assert err.startswith("transit-cal: error: SOURCE_DATE_EPOCH")
    assert "Traceback" not in err


@pytest.mark.parametrize(
    "argv",
    [
        ["build"],
        ["build", "feed.zip", "--days", "0"],
        ["build", "feed.zip", "--days", "-1"],
        ["build", "feed.zip", "--days", "abc"],
        ["build", "feed.zip", "--start", "2026-13-01"],
        ["build", "feed.zip", "--start", "tomorrow"],
        [],
    ],
)
def test_bad_usage_exits_2(calls, capsys, argv) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert calls == []
    assert "usage: transit-cal" in capsys.readouterr().err


def test_build_writes_feeds_and_prints_a_summary(gtfs_zip, agency_a, tmp_path, capsys) -> None:
    out = tmp_path / "out"
    argv = ["build", str(gtfs_zip(FROM_HUB_TRIP)), "--out", str(out), "--days", "3"]
    assert cli.main(argv) == 0
    paths = sorted(str(p.relative_to(out)) for p in out.rglob("*.ics"))
    assert paths == ["o-9q9-agencya/oak-from-hub.ics", "o-9q9-agencya/oak-to-hub.ics"]
    lines = capsys.readouterr().out.splitlines()
    # NOW is 2026-10-05 in Los Angeles, so the window starts on Sunday 10-04: two weekdays.
    assert lines == [
        "Agency A Ferry: service days 2026-10-04 to 2026-10-06",
        "      2 departures, last 2026-10-30  o-9q9-agencya/oak-to-hub.ics",
        "      2 departures, last 2026-10-30  o-9q9-agencya/oak-from-hub.ics",
        f"wrote 2 feeds under {out}",
    ]


def gtfs_input(kind: str, tmp_path: Path, gtfs_zip: Callable[..., Path]) -> Path:
    if kind == "missing":
        return tmp_path / "missing.zip"
    if kind == "corrupt":
        (path := tmp_path / "corrupt.zip").write_bytes(b"not a zip")
        return path
    if kind == "bad timezone":
        return gtfs_zip(
            {"agency.txt": ("https://a.test/,America/Los_Angeles", "https://a.test/,Z")}
        )
    return gtfs_zip()  # agency A has no trips from the hub


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("missing", "No such file"),
        ("corrupt", "corrupt.zip is not a readable zip"),
        ("bad timezone", "timezone 'Z'"),
        ("no return trip", "oak-from-hub.ics: no departures"),
    ],
)
def test_expected_failures_exit_1_with_one_line_and_write_nothing(
    gtfs_zip, agency_a, tmp_path, capsys, kind, message
) -> None:
    out = tmp_path / "out"
    assert cli.main(["build", str(gtfs_input(kind, tmp_path, gtfs_zip)), "--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("transit-cal: error: ")
    assert message in err
    assert err.count("\n") == 1
    assert not out.exists()


def test_unexpected_errors_are_not_swallowed(monkeypatch) -> None:
    def broken(*args: object, **kwargs: object) -> list[FeedResult]:
        raise RuntimeError("bug")

    monkeypatch.setattr(cli, "build_feeds", broken)
    with pytest.raises(RuntimeError, match="bug"):
        cli.main(["build", "feed.zip"])


def test_build_error_message_is_printed_as_is(monkeypatch, capsys) -> None:
    def fail(*args: object, **kwargs: object) -> list[FeedResult]:
        raise BuildError("feed path 'x' built twice")

    monkeypatch.setattr(cli, "build_feeds", fail)
    assert cli.main(["build", "feed.zip"]) == 1
    assert capsys.readouterr().err == "transit-cal: error: feed path 'x' built twice\n"


def test_operators_option_builds_the_synthetic_fixture_end_to_end(
    synthetic_gtfs, tmp_path, capsys
) -> None:
    out = tmp_path / "out"
    argv = ["build", str(synthetic_gtfs), "--operators", str(SYNTHETIC / "operators")]
    argv += ["--out", str(out), "--start", "2026-10-06", "--days", "7"]
    assert cli.main(argv) == 0
    paths = sorted(str(p.relative_to(out)) for p in out.rglob("*.ics"))
    assert paths == [
        f"o-9q9-exampletransit/{name}.ics"
        for name in [
            "east-from-central",
            "east-to-central",
            "north-from-central",
            "north-to-central",
            "south-line-from-south",
            "south-line-to-south",
        ]
    ]
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Example Transit: service days 2026-10-06 to 2026-10-12"
    assert lines[-1] == f"wrote 6 feeds under {out}"


def operators_input(kind: str, tmp_path: Path) -> Path:
    path = tmp_path / "operators"
    if kind == "a file":
        path.write_text("not a directory", encoding="utf-8")
    elif kind != "missing":
        path.mkdir()
    if kind == "malformed":
        (path / "broken.toml").write_text("name = ", encoding="utf-8")
    if kind == "not UTF-8":
        (path / "latin.toml").write_bytes(b'name = "Caf\xe9"\n')
    return path


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("missing", "No such file"),
        ("a file", "Not a directory"),
        ("empty", "no operators to build"),
        ("malformed", "broken.toml: "),
        ("not UTF-8", "latin.toml: not UTF-8"),
    ],
)
def test_bad_operators_directory_exits_1_with_one_line_and_writes_nothing(
    synthetic_gtfs, tmp_path, capsys, kind, message
) -> None:
    out = tmp_path / "out"
    argv = ["build", str(synthetic_gtfs), "--operators", str(operators_input(kind, tmp_path))]
    assert cli.main([*argv, "--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("transit-cal: error: ")
    assert message in err
    assert err.count("\n") == 1
    assert not out.exists()
