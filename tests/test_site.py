import json
from pathlib import Path

import pytest

from transit_cal import cli
from transit_cal.build import BuildError, feed_specs
from transit_cal.catalog import load_operators
from transit_cal.site import site_data

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic" / "operators"


def test_site_data_lists_the_synthetic_catalog_in_catalog_order() -> None:
    assert site_data(load_operators(SYNTHETIC)) == {
        "operators": [
            {
                "slug": "example-transit",
                "name": "Example Transit",
                "website": "https://example.test/",
                "onestop_id": "o-9q9-exampletransit",
                "routes": [
                    {
                        "slug": "north",
                        "name": "North",
                        "url": "https://example.test/north/",
                        "feeds": [
                            {
                                "name": "To Central",
                                "path": "o-9q9-exampletransit/north-to-central.ics",
                            },
                            {
                                "name": "From Central",
                                "path": "o-9q9-exampletransit/north-from-central.ics",
                            },
                        ],
                    },
                    {
                        "slug": "east",
                        "name": "East",
                        "url": "https://example.test/east/",
                        "feeds": [
                            {
                                "name": "To Central",
                                "path": "o-9q9-exampletransit/east-to-central.ics",
                            },
                            {
                                "name": "From Central",
                                "path": "o-9q9-exampletransit/east-from-central.ics",
                            },
                        ],
                    },
                    {
                        "slug": "south-line",
                        "name": "South Line",
                        "url": "https://example.test/south-line/",
                        "feeds": [
                            {
                                "name": "To South",
                                "path": "o-9q9-exampletransit/south-line-to-south.ics",
                            },
                            {
                                "name": "From South",
                                "path": "o-9q9-exampletransit/south-line-from-south.ics",
                            },
                        ],
                    },
                ],
            }
        ]
    }


def test_site_data_lists_every_shipped_feed_once() -> None:
    operators = load_operators()
    listed = [
        f["path"]
        for op in site_data(operators)["operators"]
        for r in op["routes"]
        for f in r["feeds"]
    ]
    built = [s.path for op in operators for r in op.routes for s in feed_specs(op, r)]
    assert listed == built
    assert len(set(listed)) == len(listed)


def test_site_data_with_no_operators_raises() -> None:
    with pytest.raises(BuildError, match="no operators to list"):
        site_data([])


def test_site_data_command_writes_identical_json_each_run(tmp_path: Path) -> None:
    out = tmp_path / "data" / "catalog.json"
    argv = ["site-data", str(out), "--operators", str(SYNTHETIC)]

    assert cli.main(argv) == 0
    first = out.read_bytes()
    assert cli.main(argv) == 0

    assert out.read_bytes() == first
    assert json.loads(first) == site_data(load_operators(SYNTHETIC))
    assert first.endswith(b"}\n")
    assert [p.name for p in out.parent.iterdir()] == ["catalog.json"]


def test_site_data_command_with_a_bad_catalog_exits_1_and_keeps_the_old_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    operators = tmp_path / "operators"
    operators.mkdir()
    (operators / "broken.toml").write_text("name = ", encoding="utf-8")
    out = tmp_path / "catalog.json"
    out.write_text("old", encoding="utf-8")

    assert cli.main(["site-data", str(out), "--operators", str(operators)]) == 1

    err = capsys.readouterr().err
    assert err.startswith("transit-cal: error: broken.toml: ")
    assert err.count("\n") == 1
    assert out.read_text(encoding="utf-8") == "old"
