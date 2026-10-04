import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

# Agencies A and B share service WK and stop oak; X is never requested and has malformed rows.
GTFS = {
    "agency.txt": """\
agency_id,agency_name,agency_url,agency_timezone
A,Agency A,https://a.test/,America/Los_Angeles
B,Agency B,https://b.test/,America/Los_Angeles
X,Agency X,https://x.test/,America/Los_Angeles
""",
    "routes.txt": """\
route_id,agency_id,route_type
A1,A,4
B1,B,4
X1,X,3
""",
    "stops.txt": """\
stop_id,stop_name,parent_station,stop_desc,stop_lat,stop_lon
hub,Ferry Terminal,,,37.79553,-122.39341
gate,Gate A,hub,,37.79439,-122.39093
oak,Oakland,,"1 Main St, Oakland",37.79509,-122.27976
far,Far Point,,,,
xs,Other Stop,,,north,south
""",
    "calendar.txt": """\
service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
WK,1,1,1,1,1,0,0,20260629,20261101
WE,0,0,0,0,0,1,1,20260629,20261101
SP,0,0,0,0,0,0,0,20260908,20260925
XS,9,9,9,9,9,9,9,20270101,20260101
""",
    "calendar_dates.txt": """\
service_id,date,exception_type
WK,20260908,2
SP,20260908,1
XS,20260908,7
""",
    "trips.txt": """\
route_id,service_id,trip_id,block_id
A1,WK,a1,blk
A1,SP,a2,
B1,WK,b1,
B1,WE,b2,
X1,XS,x1,
""",
    "stop_times.txt": """\
trip_id,stop_id,stop_sequence,arrival_time,departure_time
a1,gate,2,06:20:00,06:20:00
a1,oak,1,05:55:00,05:55:00
a2,oak,1,24:05:00,24:05:00
a2,gate,2,24:40:00,24:40:00
b1,oak,1,07:00:00,07:00:00
b1,far,2,07:30:00,07:30:00
b2,far,1,09:00:00,09:00:00
x1,xs,1,never,never
""",
}

Edit = str | tuple[str, str] | None


@pytest.fixture
def gtfs_zip(tmp_path: Path) -> Callable[..., Path]:
    """Write the GTFS above to a zip. Edits are new text, an (old, new) pair, or None to drop."""

    def write(edits: dict[str, Edit] | None = None, *, bom: bool = False) -> Path:
        files: dict[str, str | None] = dict(GTFS)
        for name, edit in (edits or {}).items():
            if isinstance(edit, tuple):
                assert edit[0] in GTFS[name], edit[0]
                edit = GTFS[name].replace(*edit)
            files[name] = edit
        path = tmp_path / "gtfs.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, text in files.items():
                if text is not None:
                    zf.writestr(name, ("\ufeff" if bom else "") + text)
        return path

    return write
