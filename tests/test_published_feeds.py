"""Published feed paths are permanent: each one keeps its path and the route it was published for.

`published-feeds.json` lists every path ever published with its owner (GTFS route, direction and
terminals). A new feed needs a new entry; removing, renaming or repointing one fails here. A failure
means a subscriber's URL would break or start showing a different route. Only update an entry when
the owner is the same route under new GTFS ids, never to reuse a path.
"""

import json
from pathlib import Path

import pytest

from transit_cal.catalog import load_operators
from transit_cal.ics import FeedSpec
from transit_cal.legs import FROM_HUB, TO_HUB

MANIFEST = Path(__file__).parent / "published-feeds.json"
Owner = dict[str, str | list[str]]


def current_feeds() -> dict[str, Owner]:
    feeds = {}
    for op in load_operators():
        for r in op.routes:
            for direction, d in ((TO_HUB, r.hub.to), (FROM_HUB, r.hub.from_)):
                spec = FeedSpec(op.onestop_id, "", r.slug, "", direction, d.slug, "", "", "")
                feeds[spec.path] = {
                    "gtfs_route": r.gtfs_route,
                    "direction": direction,
                    "terminals": sorted(r.terminals),
                }
    return feeds


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [k for k, _ in pairs]
    if twice := sorted({k for k in keys if keys.count(k) > 1}):
        raise ValueError(f"{twice[0]!r} listed twice")
    return dict(pairs)


def parse(text: str) -> dict[str, Owner]:
    """The manifest as {path: owner}. Raises ValueError for a path listed twice."""
    return json.loads(text, object_pairs_hook=_unique)


def published() -> dict[str, Owner]:
    return parse(MANIFEST.read_text(encoding="utf-8"))


def test_every_published_feed_keeps_its_path_and_owner() -> None:
    current = current_feeds()
    changed = {p: (o, current.get(p)) for p, o in published().items() if current.get(p) != o}
    assert not changed


def test_manifest_lists_every_current_feed() -> None:
    assert current_feeds().keys() <= published().keys()


def test_a_path_listed_twice_is_rejected() -> None:
    with pytest.raises(ValueError, match="'a.ics' listed twice"):
        parse('{"a.ics": {"gtfs_route": "old"}, "a.ics": {"gtfs_route": "new"}}')
