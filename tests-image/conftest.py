"""Fixtures for the production image tests: a served container and an empty-volume one.

Run by bin/test-image against the tag bin/build-image builds. bin/test never collects these.
"""

import os
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import HTTPConnection
from pathlib import Path

import pytest

from transit_cal.schedule import next_slot

IMAGE = os.environ.get("IMAGE", "transit-cal:local")
RELEASE = Path("o-test-ferry/oak-to-hub.ics")
WAIT_SECONDS = 10


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def wait_until[T](probe: Callable[[], T | None], message: str, timeout: float = WAIT_SECONDS) -> T:
    """Poll probe until it returns a truthy value, and return that value; fail after timeout."""
    deadline = time.monotonic() + timeout
    while True:
        if value := probe():
            return value
        if time.monotonic() > deadline:
            pytest.fail(f"{message} after {timeout:g}s")
        time.sleep(0.5)


@dataclass(frozen=True)
class Response:
    status: int
    content_type: str  # The media type, or "" when the header is missing
    headers: dict[str, str] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Container:
    name: str
    port: int

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def exec(
        self, *args: str, user: str | None = None, privileged: bool = False, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        flags = [*(["--user", user] if user else []), *(["--privileged"] if privileged else [])]
        return docker("exec", *flags, self.name, *args, check=check)

    def logs(self) -> str:
        out = subprocess.run(
            ["docker", "logs", self.name], check=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
        )  # fmt: skip
        return out.stdout

    def pids(self, pattern: str) -> list[str]:
        return self.exec("pgrep", "-f", pattern, check=False).stdout.split()

    def pid_of(self, pattern: str) -> str:
        """The newest pid whose command line matches pattern, waiting for one to appear."""
        return wait_until(
            lambda: self.exec("pgrep", "-n", "-f", pattern, check=False).stdout.strip(),
            f"no process matching {pattern!r}",
        )

    def get(self, path: str) -> Response:
        """GET path byte for byte, with dot segments intact and no redirects followed."""
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            response.read()
            header = response.getheader("Content-Type") or ""
            headers = {name.lower(): value for name, value in response.getheaders()}
            return Response(response.status, header.split(";")[0].strip(), headers)
        finally:
            connection.close()

    def healthy(self) -> bool:
        try:
            return self.get("/healthz").status == 200
        except OSError:
            return False


def start(name: str, *flags: str) -> Container:
    """Run IMAGE read-only, published on a loopback port Docker picks, and wait until it serves."""
    docker(
        "run", "--detach", "--name", name, "--read-only", "--tmpfs", "/tmp",
        "--publish", "127.0.0.1::8080", *flags, IMAGE,
    )  # fmt: skip
    port = docker("port", name, "8080/tcp").stdout.splitlines()[0].rsplit(":", 1)[1]
    container = Container(name, int(port))
    try:
        wait_until(container.healthy, f"{name} not serving /healthz")
    except pytest.fail.Exception as failure:
        pytest.fail(f"{failure}\n\ndocker logs {name}:\n{container.logs()}", pytrace=False)
    return container


def remove(name: str) -> None:
    docker("rm", "--force", name, check=False)


@pytest.fixture(scope="session", autouse=True)
def image() -> str:
    if docker("info", check=False).returncode != 0:
        pytest.exit("Docker daemon not reachable", returncode=1)
    if docker("image", "inspect", IMAGE, check=False).returncode != 0:
        pytest.exit(f"image {IMAGE} not found; run bin/build-image", returncode=1)
    return IMAGE


@pytest.fixture(scope="session")
def release_name() -> str:
    """Named for now, so the scheduler finds the feeds fresh and waits for its next slot."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.000000Z")


@pytest.fixture(scope="session")
def feeds(tmp_path_factory: pytest.TempPathFactory, release_name: str) -> Path:
    """A published tree as tools/build-feeds leaves it, plus files that must never be served."""
    root = tmp_path_factory.mktemp("feeds")
    release = root / "releases" / release_name
    (release / RELEASE).parent.mkdir(parents=True)
    (release / RELEASE).write_bytes(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    (root / "gtfs").mkdir()
    (root / "gtfs" / "abc.zip").write_bytes(b"not served")
    (root / "staging").mkdir()
    (root / "current").symlink_to(f"releases/{release_name}")
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)
    # Sealed as publish leaves a release, so Caddy is shown serving a read-only tree
    for path in sorted(release.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    release.chmod(0o555)
    return root


@pytest.fixture(scope="session")
def server(feeds: Path) -> Iterator[Container]:
    """The image serving feeds from a read-only volume, with a key that is never used."""
    name = f"transit-cal-test-{os.getpid()}"
    try:
        yield start(name, "-v", f"{feeds}:/feeds:ro", "-e", "TRANSITLAND_API_KEY=not-a-real-key")
    finally:
        remove(name)


@pytest.fixture(scope="session")
def empty_server() -> Iterator[Container]:
    """The image on an empty volume with no key, after its catch-up build has failed."""
    now = datetime.now(UTC)
    # A slot during this check would add a second, legitimate failure, so start clear of one
    if (until := (next_slot(now) - now).total_seconds()) < 60:
        time.sleep(until + 5)
    name = f"transit-cal-test-empty-{os.getpid()}"
    try:
        container = start(name, "--tmpfs", "/feeds")
        wait_until(
            lambda: "schedule: build failed with exit status 1" in container.logs(),
            "no catch-up build failure logged",
            timeout=20,
        )
        yield container
    finally:
        remove(name)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    """Attach the logs of every container a failed test used, as the shell script printed them."""
    report = yield
    if report.failed:
        for value in getattr(item, "funcargs", {}).values():
            if isinstance(value, Container):
                try:
                    logs = value.logs()
                except subprocess.CalledProcessError as error:
                    logs = f"could not read logs: {error}"
                report.sections.append((f"docker logs {value.name}", logs))
    return report
