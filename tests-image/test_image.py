"""The production image: privileges, contents, routes, supervision and failure handling.

Tests that kill or restart processes come last, so earlier tests see the container as started.
"""

import json
import os
import subprocess
import time

import pytest
from conftest import RELEASE, Container, Response, wait_until

# Unpacked size of the root filesystem, the same on every Docker storage backend
MAX_IMAGE_MB = 350
KEY = "TRANSITLAND_API_KEY="
CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


def uid_of(server: Container, pid: str) -> str:
    return server.exec("stat", "-c", "%u", f"/proc/{pid}").stdout.strip()


def has_key(server: Container, pid: str) -> bool:
    environ = server.exec("cat", f"/proc/{pid}/environ", privileged=True).stdout
    return any(variable.startswith(KEY) for variable in environ.split("\0"))


def test_processes_run_as_their_own_users(server: Container):
    caddy = server.pid_of("^caddy run")
    scheduler = server.pid_of("transit-cal-schedule")

    assert uid_of(server, "1") == "0", "s6-svscan is not PID 1 as root"
    assert uid_of(server, caddy) == "10001"
    assert uid_of(server, scheduler) == "10002"


def test_only_the_scheduler_holds_the_transitland_key(server: Container):
    caddy = server.pid_of("^caddy run")
    scheduler = server.pid_of("transit-cal-schedule")

    for pid in ["1", *server.pids("s6-supervise"), caddy]:
        assert not has_key(server, pid), f"pid {pid} holds the Transitland key"
    assert has_key(server, scheduler), "the scheduler did not receive the Transitland key"


def test_scheduler_and_its_builds_write_python_output_unbuffered(server: Container):
    scheduler = server.pid_of("transit-cal-schedule")

    environ = server.exec("cat", f"/proc/{scheduler}/environ", privileged=True).stdout

    assert "PYTHONUNBUFFERED=1" in environ.split("\0")


def test_scheduler_umask_lets_caddy_read_releases(server: Container):
    scheduler = server.pid_of("transit-cal-schedule")

    status = server.exec("cat", f"/proc/{scheduler}/status").stdout

    assert "Umask:\t0022" in status.splitlines()


def test_image_has_no_jdk(server: Container):
    assert server.exec("sh", "-c", "command -v javac", check=False).returncode != 0


def test_image_unpacks_within_budget(server: Container):
    size = int(server.exec("du", "-sxm", "/", user="0").stdout.split()[0])

    assert size <= MAX_IMAGE_MB, f"image unpacks to {size} MB, over {MAX_IMAGE_MB} MB"


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/healthz", "text/plain"),
        ("/", "text/html"),
        ("/css/pico.min.css", "text/css"),
        (f"/{RELEASE}", "text/calendar"),
    ],
)
def test_route_serves_its_content_type(server: Container, path: str, content_type: str):
    assert server.get(path) == Response(200, content_type)


@pytest.mark.parametrize(
    "path",
    [
        f"/current/{RELEASE}",
        f"/releases/{{release}}/{RELEASE}",
        "/gtfs/abc.zip",
        "/o-test-ferry/../gtfs/abc.zip",
    ],
)
def test_build_internals_are_never_served(server: Container, release_name: str, path: str):
    assert server.get(path.format(release=release_name)).status == 404


@pytest.mark.parametrize("path", ["/", f"/{RELEASE}"])
def test_responses_carry_hsts_and_a_strict_content_security_policy(server: Container, path: str):
    headers = server.get(path).headers

    assert headers["strict-transport-security"] == "max-age=31536000"
    assert headers["content-security-policy"] == CSP


def test_no_file_in_the_image_is_setuid_or_setgid(server: Container):
    found = server.exec(
        "find", "/", "-xdev", "-type", "f", "(", "-perm", "-4000", "-o", "-perm", "-2000", ")",
        user="0",
    )  # fmt: skip

    assert found.stdout.split() == []


def test_playwright_specs_pass_against_the_image(server: Container):
    run = subprocess.run(
        ["pnpm", "--dir", "web", "exec", "playwright", "test"],
        env={**os.environ, "BASE_URL": server.base},
        check=False,
    )

    assert run.returncode == 0


def test_scheduler_waits_for_its_slot_when_feeds_are_fresh(server: Container):
    wait_until(
        lambda: "schedule: next build at " in server.logs(),
        "the scheduler never announced its next build",
    )

    assert "schedule: feeds missing" not in server.logs()


def test_crash_delay_applies_only_to_a_crash(server: Container):
    def finish(*args: str) -> str:
        script = 'sleep() { echo "slept $1"; }; . /etc/s6/scheduler/finish'
        return server.exec("sh", "-c", script, "sh", *args).stdout.strip()

    assert finish("1", "0") == "slept 60", "a crashed scheduler restarts without a delay"
    assert finish("256", "15") == "", "a stopped scheduler waits before restarting"


def test_s6_restarts_a_killed_caddy(server: Container):
    caddy = server.pid_of("^caddy run")

    server.exec("kill", "-KILL", caddy)

    wait_until(
        lambda: server.pid_of("^caddy run") != caddy and server.healthy(),
        "caddy not serving again after it was killed",
    )


def test_s6_restarts_a_stopped_scheduler(server: Container):
    scheduler = server.pid_of("transit-cal-schedule")

    server.exec("kill", "-TERM", scheduler)

    wait_until(
        lambda: [p for p in server.pids("transit-cal-schedule") if p != scheduler],
        "the scheduler was not restarted after SIGTERM",
    )


# After the restarts, so it also judges what killing and restarting the services logged
def test_server_logs_no_warnings_or_errors(server: Container):
    # Each Caddy start cleans storage in a background goroutine; wait for every start's cleanup
    def cleaned() -> bool:
        logs = server.logs()
        starts = logs.count('"msg":"serving initial configuration"')
        return 0 < starts <= logs.count('"msg":"finished cleaning storage units"')

    wait_until(cleaned, "storage cleanup not logged for every Caddy start")
    problems = []
    for line in server.logs().splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        # The admin API is off on purpose
        if entry.get("level") in {"warn", "error"} and entry["msg"] != "admin endpoint disabled":
            problems.append(line)

    assert not problems


def test_failed_catch_up_build_names_the_missing_key(empty_server: Container):
    assert "TRANSITLAND_API_KEY is empty" in empty_server.logs()


def test_failed_catch_up_build_leaves_a_servable_tree(empty_server: Container):
    # Made by the scheduler's own build before the fetch failed, so Caddy must traverse it
    assert empty_server.exec("stat", "-c", "%a", "/feeds/staging").stdout.strip() == "755"


def test_failed_catch_up_build_waits_for_the_next_slot(empty_server: Container):
    time.sleep(3)

    assert empty_server.logs().count("schedule: build failed") == 1


def test_caddy_serves_after_a_failed_build(empty_server: Container):
    assert empty_server.healthy()
