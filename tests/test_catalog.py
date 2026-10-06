from pathlib import Path

import pytest

from transit_cal.catalog import CatalogError, load_operators

SF = "mtc:san-francisco-ferry-terminal"
VALID = """\
name = "Test Ferry"
gtfs_agency = "TF"
onestop_id = "o-test-ferry"
website = "https://ferry.test/"

[hub]
station = "hub"
to = { slug = "to-hub", name = "To Hub" }
from = { slug = "from-hub", name = "From Hub" }

[[routes]]
slug = "north"
name = "North"
gtfs_route = "TF:N"
url = "https://ferry.test/north/"
terminals = ["n1", "n2"]

[[routes]]
slug = "south"
name = "South"
gtfs_route = "TF:S"
url = "https://ferry.test/south/"
terminals = ["s1"]

[routes.hub]
station = "s0"
to = { slug = "to-s0", name = "To S0" }
from = { slug = "from-s0", name = "From S0" }

[stops.n1]
name = "North One"
gate = "Gate A"
"""


def write(tmp_path: Path, text: str, name: str = "test-ferry.toml") -> Path:
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_shipped_operators_load_with_hubs_and_terminals() -> None:
    operators = load_operators()
    assert [op.slug for op in operators] == ["golden-gate-ferry", "sf-bay-ferry"]
    for op in operators:
        assert all(r.terminals for r in op.routes), op.slug
    sb = operators[1]
    routes = {r.slug: r for r in sb.routes}
    assert sb.other_hubs(routes["harbor-bay"]) == {"7205", "7215"}
    assert sb.other_hubs(routes["south-san-francisco"]) == {SF, "7215"}
    assert operators[0].other_hubs(operators[0].routes[0]) == set()


def test_routes_use_the_operator_hub_unless_they_have_their_own(tmp_path: Path) -> None:
    (op,) = load_operators(write(tmp_path, VALID))
    north, south = op.routes
    assert (north.hub, south.hub.station, south.hub.from_.slug) == (op.hub, "s0", "from-s0")
    assert op.other_hubs(north) == {"s0"}
    assert north.terminals == {"n1", "n2"}
    assert (op.stops["n1"].gate, op.stops["n1"].address) == ("Gate A", "")


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        (
            'website = "https://ferry.test/"',
            'website = "x"\ncolour = "red"',
            "unknown key 'colour'",
        ),
        ('gate = "Gate A"', 'gate = "Gate A"\nfloor = 2', r"stops\.n1: unknown key 'floor'"),
        ('name = "Test Ferry"\n', "", "missing key 'name'"),
        ('terminals = ["s1"]', 'terminals = "s1"', r"routes\[1\]\.terminals: expected a list"),
        ('url = "https://ferry.test/north/"', "url = 7", r"routes\[0\]\.url: expected a string"),
        ('terminals = ["s1"]', "terminals = []", "no terminals"),
        (
            'terminals = ["s1"]',
            'terminals = ["s1", "s1"]',
            r"routes\[1\]\.terminals: 's1' listed twice",
        ),
        ('slug = "south"', 'slug = "north"', "route slug 'north' used twice"),
        ('slug = "south"', 'slug = "../south"', "bad slug '../south'"),
        ('slug = "from-hub"', 'slug = "to-hub"', "to and from slugs are both 'to-hub'"),
        ('slug = "to-s0"', 'slug = "to-south"', "direction 'to-south' repeats route 'south'"),
        ('slug = "from-s0"', 'slug = "from-south"', "direction 'from-south' repeats route 'south'"),
        ('slug = "from-s0"', 'slug = "s0"', "direction 's0' must start with to- or from-"),
        ('slug = "to-s0"', 'slug = "towards-s0"', "direction 'towards-s0' must start with to- or"),
        ("[hub]", "[hub", "test-ferry.toml"),
        ('onestop_id = "o-test-ferry"', 'onestop_id = "o-x/../../etc"', "bad onestop_id"),
        ('onestop_id = "o-test-ferry"', 'onestop_id = "O-Test-Ferry"', "bad onestop_id"),
        ('onestop_id = "o-test-ferry"', 'onestop_id = "r-test-ferry"', "bad onestop_id"),
        ('onestop_id = "o-test-ferry"', 'onestop_id = "o-9q9p-a-b"', "bad onestop_id"),
    ],
)
def test_bad_operator_file_raises(tmp_path: Path, old: str, new: str, match: str) -> None:
    assert old in VALID, old
    with pytest.raises(CatalogError, match=match):
        load_operators(write(tmp_path, VALID.replace(old, new, 1)))


def test_bad_file_name_raises(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="bad slug 'Test_Ferry'"):
        load_operators(write(tmp_path, VALID, "Test_Ferry.toml"))


def test_two_operators_with_one_gtfs_agency_raise(tmp_path: Path) -> None:
    write(tmp_path, VALID, "another-ferry.toml")
    with pytest.raises(
        CatalogError, match="test-ferry.toml: gtfs_agency 'TF' also used by another-ferry.toml"
    ):
        load_operators(write(tmp_path, VALID))


def test_routes_that_are_not_a_list_raise(tmp_path: Path) -> None:
    text = VALID.split("[[routes]]")[0].replace("[hub]", "routes = 7\n\n[hub]")
    with pytest.raises(CatalogError, match=r"test-ferry\.toml\.routes: expected a list"):
        load_operators(write(tmp_path, text))


@pytest.mark.parametrize("onestop_id", ["o-9q9p-sanfranciscobayferry", "o-sf~bay", "o-test-ferry"])
def test_onestop_ids_in_transitland_form_load(tmp_path: Path, onestop_id: str) -> None:
    text = VALID.replace('"o-test-ferry"', f'"{onestop_id}"')
    assert load_operators(write(tmp_path, text))[0].onestop_id == onestop_id


def test_feed_names_that_repeat_within_an_operator_raise(tmp_path: Path) -> None:
    # north + to-hub-to-s0 and north-to-hub + to-s0 both name the feed north-to-hub-to-s0.ics.
    text = VALID.replace('slug = "south"', 'slug = "north-to-hub"').replace(
        'to = { slug = "to-hub"', 'to = { slug = "to-hub-to-s0"'
    )
    with pytest.raises(CatalogError, match="feed name 'north-to-hub-to-s0' used twice"):
        load_operators(write(tmp_path, text))


def test_two_operators_with_one_onestop_id_raise(tmp_path: Path) -> None:
    write(tmp_path, VALID.replace('gtfs_agency = "TF"', 'gtfs_agency = "AF"'), "another-ferry.toml")
    with pytest.raises(
        CatalogError,
        match="test-ferry.toml: onestop_id 'o-test-ferry' also used by another-ferry.toml",
    ):
        load_operators(write(tmp_path, VALID))


def test_a_direction_may_share_a_word_with_its_route(tmp_path: Path) -> None:
    text = VALID.replace('slug = "to-s0"', 'slug = "to-southbank"')
    assert load_operators(write(tmp_path, text))[0].routes[1].hub.to.slug == "to-southbank"


def test_an_operator_hub_every_route_overrides_still_needs_direction_prefixes(
    tmp_path: Path,
) -> None:
    text = VALID.replace('slug = "to-hub"', 'slug = "hub"').replace(
        'terminals = ["n1", "n2"]\n',
        'terminals = ["n1", "n2"]\n\n[routes.hub]\nstation = "n0"\n'
        'to = { slug = "to-n0", name = "To N0" }\nfrom = { slug = "from-n0", name = "From N0" }\n',
    )
    with pytest.raises(CatalogError, match="direction 'hub' must start with to- or from-"):
        load_operators(write(tmp_path, text))
