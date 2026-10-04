"""The operators we publish: one TOML file each in `operators/`, read into typed records."""

import re
import tomllib
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from types import NoneType, UnionType
from typing import Any, get_args, get_origin, get_type_hints

# Slugs become path segments in feed URLs.
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
# A Transitland operator Onestop ID, o-<geohash>-<name> or o-<name>: the first URL segment.
ONESTOP_ID = re.compile(r"o(-[0-9a-z~]+){1,2}")


class CatalogError(ValueError):
    """An operator file is malformed or breaks a catalog rule."""


@dataclass(frozen=True)
class Direction:
    slug: str
    name: str


@dataclass(frozen=True)
class Hub:
    """Where a route's legs start or end, and how its two directions are named."""

    station: str
    to: Direction
    from_: Direction


@dataclass(frozen=True)
class Route:
    slug: str
    name: str
    gtfs_route: str
    url: str
    terminals: frozenset[str]
    hub: Hub


@dataclass(frozen=True)
class StopInfo:
    """How a stop is shown: name in titles; optional gate, address and map title."""

    name: str
    gate: str = ""
    address: str = ""
    place: str = ""


@dataclass(frozen=True)
class Operator:
    name: str
    gtfs_agency: str
    onestop_id: str
    website: str
    hub: Hub
    routes: tuple[Route, ...]
    stops: dict[str, StopInfo] = field(default_factory=dict)
    slug: str = ""

    def other_hubs(self, route: Route) -> set[str]:
        """The operator's other hub stations, where this route's runs are also cut."""
        return {r.hub.station for r in self.routes} - {route.hub.station}


def load_operators(directory: Traversable | Path | None = None) -> list[Operator]:
    """Load every operator file, sorted by slug. Raises CatalogError naming the file and key."""
    directory = directory or files("transit_cal") / "operators"
    operators = []
    for path in sorted((p for p in directory.iterdir() if p.name.endswith(".toml")), key=str):
        slug = path.name.removesuffix(".toml")
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise CatalogError(f"{path.name}: {e}") from None
        if "slug" in data:
            raise CatalogError(f"{path.name}: unknown key 'slug' (the file name is the slug)")
        routes = data.get("routes")
        for route in routes if isinstance(routes, list) else []:  # default to the operator's hub
            if isinstance(route, dict) and "hub" in data:
                route.setdefault("hub", data["hub"])
        operator: Operator = _decode(Operator, data | {"slug": slug}, path.name)
        _check(operator, path.name)
        for attr in ("gtfs_agency", "onestop_id"):
            value = getattr(operator, attr)
            if other := next((o for o in operators if getattr(o, attr) == value), None):
                raise CatalogError(f"{path.name}: {attr} {value!r} also used by {other.slug}.toml")
        operators.append(operator)
    return operators


def _check(op: Operator, where: str) -> None:
    hubs = [op.hub, *(r.hub for r in op.routes)]
    slugs = [
        op.slug,
        *(r.slug for r in op.routes),
        *(d.slug for h in hubs for d in (h.to, h.from_)),
    ]
    if bad := [s for s in slugs if not SLUG.fullmatch(s)]:
        raise CatalogError(f"{where}: bad slug {bad[0]!r}")
    if not ONESTOP_ID.fullmatch(op.onestop_id):
        raise CatalogError(f"{where}: bad onestop_id {op.onestop_id!r}")
    for h in hubs:
        if h.to.slug == h.from_.slug:
            raise CatalogError(f"{where}: to and from slugs are both {h.to.slug!r}")
    routes = [r.slug for r in op.routes]
    if twice := sorted({s for s in routes if routes.count(s) > 1}):
        raise CatalogError(f"{where}: route slug {twice[0]!r} used twice")
    names = [f"{r.slug}-{d.slug}" for r in op.routes for d in (r.hub.to, r.hub.from_)]
    if twice := sorted({n for n in names if names.count(n) > 1}):
        raise CatalogError(f"{where}: feed name {twice[0]!r} used twice")
    if empty := [r.slug for r in op.routes if not r.terminals]:
        raise CatalogError(f"{where}: route {empty[0]!r} has no terminals")


def _decode(cls: Any, data: Any, where: str) -> Any:
    """Build a dataclass from a TOML table, rejecting unknown, missing and mistyped keys."""
    if not isinstance(data, dict):
        raise CatalogError(f"{where}: expected a table")
    names = {f.name.rstrip("_"): f for f in fields(cls)}
    if unknown := sorted(data.keys() - names.keys()):
        raise CatalogError(f"{where}: unknown key {unknown[0]!r}")
    hints = get_type_hints(cls)
    values = {}
    for key, f in names.items():
        if key in data:
            values[f.name] = _value(hints[f.name], data[key], f"{where}.{key}")
        elif f.default is MISSING and f.default_factory is MISSING:
            raise CatalogError(f"{where}: missing key {key!r}")
    return cls(**values)


def _value(hint: Any, data: Any, where: str) -> Any:
    origin, args = get_origin(hint), get_args(hint)
    if origin is UnionType:  # an optional table, present here
        (hint,) = (a for a in args if a is not NoneType)
        return _value(hint, data, where)
    if is_dataclass(hint):
        return _decode(hint, data, where)
    if origin in (tuple, frozenset):
        if not isinstance(data, list):
            raise CatalogError(f"{where}: expected a list")
        items = [_value(args[0], v, f"{where}[{i}]") for i, v in enumerate(data)]
        if origin is tuple:
            return tuple(items)
        if twice := sorted({v for v in items if items.count(v) > 1}):
            raise CatalogError(f"{where}: {twice[0]!r} listed twice")
        return frozenset(items)
    if origin is dict:
        if not isinstance(data, dict):
            raise CatalogError(f"{where}: expected a table")
        return {k: _value(args[1], v, f"{where}.{k}") for k, v in data.items()}
    if not isinstance(data, str):
        raise CatalogError(f"{where}: expected a string")
    return data
