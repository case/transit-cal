"""Run build-feeds at fixed UTC slots, and at startup when the served feeds are stale.

Long-running under the image's supervisor, so it imports only the standard library and publish.
"""

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import NoReturn

from transit_cal.publish import CURRENT, RELEASE_NAME

SLOT_HOURS = (5, 11, 17, 23)
MAX_AGE = timedelta(hours=12)
# Short sleeps, so a clock change or a suspended host cannot carry the loop past a slot
MAX_SLEEP_SECONDS = 300
TERM_GRACE_SECONDS = 10
# A normal build takes ~15 s; a server dripping bytes could otherwise hold one open forever
MAX_BUILD_SECONDS = 30 * 60
COMMAND = ("build-feeds",)


def next_slot(now: datetime) -> datetime:
    """The first build slot strictly after now, in UTC."""
    now = now.astimezone(UTC)
    for days in (0, 1):
        day = (now + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        for hour in SLOT_HOURS:
            slot = day.replace(hour=hour)
            if slot > now:
                return slot
    raise AssertionError("unreachable: tomorrow always has a slot")


def is_stale(current: Path, now: datetime) -> bool:
    """True unless current links to an existing release named for less than MAX_AGE before now."""
    try:
        if not current.is_symlink() or not current.resolve(strict=True).is_dir():
            return True
        name = Path(os.readlink(current)).name
        published = datetime.strptime(name, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
    except OSError, ValueError:
        return True
    return not RELEASE_NAME.fullmatch(name) or now - published >= MAX_AGE


class Stop(Exception):
    """SIGTERM arrived: stop any build, then exit."""


class Build:
    """One build-feeds run in its own process group, so stopping it stops its children too.

    on_sigterm is the SIGTERM handler. It raises Stop into ordinary code, never cleaning up
    itself, and defers while a process is being created so the new process is always tracked.
    """

    def __init__(self, command: Sequence[str], timeout: float) -> None:
        self.command = list(command)
        self.timeout = timeout
        self.process: subprocess.Popen[bytes] | None = None
        self._spawning = False
        self._stop_pending = False
        self._stopping = False

    def __call__(self) -> int:
        """Run the build to completion, or stop it once it passes timeout; returns its status."""
        self.start()
        try:
            return self.wait(self.timeout)
        except subprocess.TimeoutExpired:
            _log(f"schedule: build ran past {self.timeout:g} seconds, stopping it")
            self.terminate(TERM_GRACE_SECONDS)
            return self.wait()

    def on_sigterm(self, signum: int, frame: FrameType | None) -> None:
        if self._stopping:
            return
        self._stopping = True
        if self._spawning:
            self._stop_pending = True
            return
        raise Stop

    def start(self) -> None:
        self._spawning = True
        try:
            self.process = subprocess.Popen(self.command, start_new_session=True)
        finally:
            self._spawning = False
        if self._stop_pending:
            raise Stop

    def wait(self, timeout: float | None = None) -> int:
        assert self.process is not None, "wait() before start()"
        return self.process.wait(timeout)

    def terminate(self, grace: float) -> None:
        """SIGTERM the whole group, then SIGKILL whatever of it outlives grace seconds."""
        process = self.process
        if process is None:
            return
        group = process.pid
        _signal_group(group, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            process.poll()
            if not _group_alive(group):
                break
            time.sleep(0.05)
        else:
            _signal_group(group, signal.SIGKILL)
        process.wait()


# macOS answers EPERM for a group whose remaining members are all zombies; treat it as gone
def _signal_group(group: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, sig)


def _group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError, PermissionError:
        return False
    return True


def run_forever(
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    build: Callable[[], int],
    current: Path,
    log: Callable[[str], None],
) -> NoReturn:
    """Build now if current is stale, then at every slot. A failed build waits for the next slot."""
    if is_stale(current, clock()):
        hours = MAX_AGE // timedelta(hours=1)
        log(f"schedule: feeds missing or at least {hours} hours old, building now")
        _report(build(), log)
    while True:
        slot = next_slot(clock())
        log(f"schedule: next build at {slot:%Y-%m-%dT%H:%M:%SZ}")
        while (left := (slot - clock()).total_seconds()) > 0:
            sleep(min(left, MAX_SLEEP_SECONDS))
        _report(build(), log)


def _report(status: int, log: Callable[[str], None]) -> None:
    log(
        "schedule: build succeeded"
        if status == 0
        else f"schedule: build failed with exit status {status}"
    )


def main() -> NoReturn:
    """Entry point for transit-cal-schedule. SIGTERM stops a running build, then this process."""
    build = Build(COMMAND, MAX_BUILD_SECONDS)
    signal.signal(signal.SIGTERM, build.on_sigterm)
    current = Path(os.environ.get("FEEDS_DIR", "/feeds")) / CURRENT
    try:
        run_forever(lambda: datetime.now(UTC), time.sleep, build, current, _log)
    except Stop:
        _log("schedule: stopping")
        build.terminate(TERM_GRACE_SECONDS)
    # Die by the signal itself, so the supervisor sees a deliberate stop
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)
    raise AssertionError("unreachable: SIGTERM ends the process")


def _log(message: str) -> None:
    print(message, file=sys.stdout, flush=True)


if __name__ == "__main__":
    main()
