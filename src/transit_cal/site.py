"""The catalog as the website's data: every operator, route and feed path, in catalog order."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from transit_cal.build import BuildError, feed_specs, write_atomic
from transit_cal.catalog import Operator


def site_data(operators: Sequence[Operator]) -> dict[str, Any]:
    """Operators as load_operators orders them, routes in file order, to-hub feed first.

    Raises BuildError when there are no operators.
    """
    if not operators:
        raise BuildError("no operators to list")
    return {
        "operators": [
            {
                "slug": op.slug,
                "name": op.name,
                "website": op.website,
                "onestop_id": op.onestop_id,
                "routes": [
                    {
                        "slug": route.slug,
                        "name": route.name,
                        "url": route.url,
                        "feeds": [
                            {"name": s.direction_name, "path": s.path}
                            for s in feed_specs(op, route)
                        ],
                    }
                    for route in op.routes
                ],
            }
            for op in operators
        ]
    }


def write_site_data(operators: Sequence[Operator], out: Path) -> None:
    """Replace out atomically with site_data as indented JSON."""
    text = json.dumps(site_data(operators), indent=2, ensure_ascii=False) + "\n"
    write_atomic(out, text.encode())
