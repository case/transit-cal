import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from transit_cal import schedule
from transit_cal.schedule import Build, Stop, is_stale, next_slot, run_forever

T = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "want"),
    [
        (datetime(2026, 10, 5, 0, 0, tzinfo=UTC), datetime(2026, 10, 5, 5, 0, tzinfo=UTC)),
        (datetime(2026, 10, 5, 4, 59, 59, tzinfo=UTC), datetime(2026, 10, 5, 5, 0, tzinfo=UTC)),
        (datetime(2026, 10, 5, 5, 0, tzinfo=UTC), datetime(2026, 10, 5, 11, 0, tzinfo=UTC)),
        (datetime(2026, 10, 5, 16, 30, tzinfo=UTC), datetime(2026, 10, 5, 17, 0, tzinfo=UTC)),
        (datetime(2026, 10, 5, 23, 0, tzinfo=UTC), datetime(2026, 10, 6, 5, 0, tzinfo=UTC)),
        (datetime(2026, 12, 31, 23, 30, tzinfo=UTC), datetime(2027, 1, 1, 5, 0, tzinfo=UTC)),
    ],
)
def test_next_slot_is_the_first_slot_strictly_after_now(now: datetime, want: datetime):
    assert next_slot(now) == want


def test_next_slot_ignores_the_clock_zone():
    pacific = T.astimezone(timezone(timedelta(hours=-7)))

    assert next_slot(pacific) == datetime(2026, 10, 5, 17, 0, tzinfo=UTC)


def point_current_at(feeds: Path, name: str) -> Path:
    (feeds / "releases" / name).mkdir(parents=True)
    current = feeds / "current"
    current.symlink_to(f"releases/{name}")
    return current


def test_missing_current_is_stale(tmp_path: Path):
    assert is_stale(tmp_path / "current", T)


@pytest.mark.parametrize(
    ("name", "stale"),
    [
        ("20261005T000000.000001Z", False),
        ("20261005T000000.000000Z", True),
        ("20261004T000000.000000Z", True),
        ("20261005T110000.000000Z", False),
    ],
)
def test_current_older_than_12_hours_is_stale(tmp_path: Path, name: str, stale: bool):
    assert is_stale(point_current_at(tmp_path, name), T) is stale


def test_current_with_an_unreadable_release_name_is_stale(tmp_path: Path):
    assert is_stale(point_current_at(tmp_path, "notes"), T)


def test_current_that_is_a_plain_directory_is_stale(tmp_path: Path):
    (tmp_path / "current").mkdir()

    assert is_stale(tmp_path / "current", T)


class Done(Exception):
    pass


class FakeTime:
    """A clock that sleeping advances; it ends the loop once past end."""

    def __init__(self, start: datetime, end: datetime) -> None:
        self.now = start
        self.end = end
        self.sleeps: list[float] = []

    def clock(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)
        if self.now > self.end:
            raise Done


def run(fake: FakeTime, current: Path, results: list[int]) -> tuple[list[datetime], list[str]]:
    builds: list[datetime] = []
    logs: list[str] = []

    def build() -> int:
        builds.append(fake.now)
        return results.pop(0) if results else 0

    with pytest.raises(Done):
        run_forever(fake.clock, fake.sleep, build, current, logs.append)
    return builds, logs


def test_stale_feeds_build_at_startup_then_at_each_slot(tmp_path: Path):
    fake = FakeTime(T, T + timedelta(hours=13))

    builds, logs = run(fake, tmp_path / "current", [])

    assert builds == [T, T.replace(hour=17), T.replace(hour=23)]
    assert logs[0] == "schedule: feeds missing or at least 12 hours old, building now"


def test_fresh_feeds_wait_for_the_next_slot(tmp_path: Path):
    current = point_current_at(tmp_path, "20261005T110000.000000Z")
    fake = FakeTime(T, T + timedelta(hours=7))

    builds, _ = run(fake, current, [])

    assert builds == [T.replace(hour=17)]


def test_a_failed_build_waits_for_the_next_slot_without_retrying(tmp_path: Path):
    fake = FakeTime(T, T + timedelta(hours=7))

    builds, logs = run(fake, tmp_path / "current", [3, 0])

    assert builds == [T, T.replace(hour=17)]
    assert "schedule: build failed with exit status 3" in logs
    assert "schedule: build succeeded" in logs


def test_sleeps_in_short_chunks_so_a_clock_jump_cannot_skip_a_slot(tmp_path: Path):
    current = point_current_at(tmp_path, "20261005T110000.000000Z")
    fake = FakeTime(T, T + timedelta(hours=5, minutes=1))

    run(fake, current, [])

    assert max(fake.sleeps) <= 300


def test_build_logs_say_when_the_next_build_is(tmp_path: Path):
    current = point_current_at(tmp_path, "20261005T110000.000000Z")
    fake = FakeTime(T, T + timedelta(minutes=1))

    _, logs = run(fake, current, [])

    assert logs == ["schedule: next build at 2026-10-05T17:00:00Z"]


GRANDCHILD = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(60)
"""


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for(path: Path) -> int:
    deadline = time.monotonic() + 10
    while not path.exists() or not path.read_text():
        assert time.monotonic() < deadline, "the build never started its child"
        time.sleep(0.05)
    return int(path.read_text())


def test_terminate_stops_the_whole_build_process_group(tmp_path: Path):
    pid_file = tmp_path / "grandchild.pid"
    build = Build([sys.executable, "-c", GRANDCHILD, str(pid_file)], timeout=60)
    build.start()
    grandchild = wait_for(pid_file)

    build.terminate(grace=5)

    deadline = time.monotonic() + 5
    while alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(grandchild)
    assert build.wait() == -signal.SIGTERM


def test_build_returns_the_exit_status():
    build = Build([sys.executable, "-c", "raise SystemExit(4)"], timeout=60)

    assert build() == 4


def test_terminate_kills_a_build_that_ignores_sigterm(tmp_path: Path):
    ready = tmp_path / "ready"
    stubborn = (
        "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(ready)!r}, 'w').write('1'); time.sleep(60)"
    )
    build = Build([sys.executable, "-c", stubborn], timeout=60)
    build.start()
    wait_for(ready)

    build.terminate(grace=0.2)

    assert build.wait() == -signal.SIGKILL


def test_terminate_without_a_running_build_does_nothing():
    Build([sys.executable, "-c", "pass"], timeout=60).terminate(grace=1)


def test_the_scheduler_module_imports_only_the_standard_library_and_publish():
    code = (
        "import sys; import transit_cal.schedule; "
        "print(sorted(m for m in sys.modules if m.startswith('transit_cal') or m == 'icalendar'))"
    )
    out = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)

    assert out.stdout.strip() == "['transit_cal', 'transit_cal.publish', 'transit_cal.schedule']"


def test_dangling_current_with_a_fresh_name_is_stale(tmp_path: Path):
    (tmp_path / "current").symlink_to("releases/20261005T110000.000000Z")

    assert is_stale(tmp_path / "current", T)


def test_current_named_for_an_impossible_time_is_stale(tmp_path: Path):
    assert is_stale(point_current_at(tmp_path, "20261305T000000.000000Z"), T)


def test_sigterm_during_process_creation_still_tracks_and_stops_the_new_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    build = Build([sys.executable, "-c", "import time; time.sleep(60)"], timeout=60)
    real = subprocess.Popen

    def signalled_mid_spawn(command: list[str], start_new_session: bool) -> subprocess.Popen[bytes]:
        build.on_sigterm(signal.SIGTERM, None)
        return real(command, start_new_session=start_new_session)

    monkeypatch.setattr(schedule.subprocess, "Popen", signalled_mid_spawn)

    with pytest.raises(Stop):
        build.start()
    assert build.process is not None
    build.terminate(grace=5)
    assert build.wait() == -signal.SIGTERM


def test_a_second_sigterm_is_ignored_while_stopping():
    build = Build([sys.executable, "-c", "pass"], timeout=60)
    with pytest.raises(Stop):
        build.on_sigterm(signal.SIGTERM, None)

    build.on_sigterm(signal.SIGTERM, None)


LEADER_EXITS = """
import subprocess, sys, time
stubborn = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
child = subprocess.Popen([sys.executable, "-c", stubborn])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(60)
"""


def test_terminate_kills_grandchildren_that_outlive_their_leader(tmp_path: Path):
    pid_file = tmp_path / "grandchild.pid"
    build = Build([sys.executable, "-c", LEADER_EXITS, str(pid_file)], timeout=60)
    build.start()
    grandchild = wait_for(pid_file)
    time.sleep(0.2)

    build.terminate(grace=0.5)

    deadline = time.monotonic() + 5
    while alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(grandchild)


def test_sigterm_while_main_waits_on_a_build_stops_it_and_dies_by_sigterm(tmp_path: Path):
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import sys, transit_cal.schedule as s; "
        f"s.COMMAND = (sys.executable, '-c', {GRANDCHILD!r}, {str(pid_file)!r}); s.main()"
    )
    env = {**os.environ, "FEEDS_DIR": str(tmp_path)}
    scheduler = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE)
    grandchild = wait_for(pid_file)

    started = time.monotonic()
    scheduler.send_signal(signal.SIGTERM)
    out, _ = scheduler.communicate(timeout=15)

    assert scheduler.returncode == -signal.SIGTERM
    assert time.monotonic() - started < 5
    assert not alive(grandchild)
    assert "schedule: stopping" in out.decode()


def test_a_build_past_its_timeout_is_stopped_and_reported(capsys: pytest.CaptureFixture[str]):
    build = Build([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.3)
    started = time.monotonic()

    status = build()

    assert status == -signal.SIGTERM
    assert time.monotonic() - started < 5
    assert "schedule: build ran past 0.3 seconds, stopping it" in capsys.readouterr().out
