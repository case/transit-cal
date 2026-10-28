import hashlib
import importlib.machinery
import importlib.util
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "renovate-postupgrade"


def load() -> ModuleType:
    """bin/renovate-postupgrade as a module; it has no .py suffix, so the loader is explicit."""
    loader = importlib.machinery.SourceFileLoader("renovate_postupgrade", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    # No bytecode cache: it would land in bin/__pycache__, which bin/lint's bin/* globs trip on
    sys.dont_write_bytecode, before = True, sys.dont_write_bytecode
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = before
    return module


hook = load()
PinError = hook.PinError

DIGEST = "a" * 64
DOCKERFILE = f"""\
FROM eclipse-temurin:25-jdk-alpine-3.24@sha256:{DIGEST} AS validator
RUN echo from here on
FROM caddy:2.11.7@sha256:{DIGEST} AS caddy
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:{DIGEST} AS uv
FROM node:26.11.0-alpine@sha256:{DIGEST} AS build
FROM eclipse-temurin:25-jre-alpine-3.24@sha256:{DIGEST}
"""
PACKAGE_JSON = '{"name": "x", "packageManager": "pnpm@12.9.0"}\n'
MISE = """\
# Tools

[tools]
caddy = "2.11.6"
node = "26.10.0"
pnpm = "12.8.2"  # pinned
python = "3.14.8"
uv = "0.12.23"
"""
PYPROJECT = """\
[project]
name = "x"
dependencies = ["uv_build-helper>=1"]

[build-system]
requires = ["uv_build>=0.12.23,<0.13.0"]
build-backend = "uv_build"
"""
JAR = b"jar bytes"
OLD_JAR = "org/example/lib/1.0.0/lib-1.0.0.jar"
NEW_JAR = "org/example/lib/1.1.0/lib-1.1.0.jar"
KEPT_JAR = "org/other/dep/2.0.0/dep-2.0.0.jar"
OLD_SHA = "ab" * 32
KEPT_SHA = "cd" * 32
HEAD_LOCK = f"# Jars\n{OLD_SHA} {OLD_JAR}\n{KEPT_SHA} {KEPT_JAR}\n"
# What Renovate leaves: the path bumped, the hash still the old jar's
BUMPED_LOCK = f"# Jars\n{OLD_SHA} {NEW_JAR}\n{KEPT_SHA} {KEPT_JAR}\n"


def write_repo(root: Path, lock: str = HEAD_LOCK) -> Path:
    files = {
        "Dockerfile": DOCKERFILE,
        "web/package.json": PACKAGE_JSON,
        "mise.toml": MISE,
        "pyproject.toml": PYPROJECT,
        "tools/ics-validate.lock": lock,
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(text.encode())
    return root


class Maven:
    """Maven Central in memory: url -> body. Logs every fetch."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.fetched: list[str] = []

    def __call__(self, url: str, limit: int) -> bytes:
        self.fetched.append(url)
        if url not in self.files:
            raise PinError(f"{url}: 404")
        return self.files[url]


def maven_with(path: str, body: bytes, sha1: bytes | None = None) -> Maven:
    published = hashlib.sha1(body).hexdigest().encode() if sha1 is None else sha1
    return Maven({hook.MAVEN + path: body, hook.MAVEN + path + ".sha1": published})


# docker_versions


def test_docker_versions_reads_each_synced_image_and_ignores_other_froms():
    assert hook.docker_versions(DOCKERFILE) == {
        "caddy": "2.11.7",
        "node": "26.11.0",
        "uv": "0.13.1",
    }


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("FROM caddy:2.11.7 AS caddy", "is not FROM"),
        ("FROM caddy:${CADDY}@sha256:" + DIGEST, "is not FROM"),
        (f"FROM --platform=linux/amd64 caddy:2.11.7@sha256:{DIGEST}", "is not FROM"),
        (f"  from caddy:2.11.7@sha256:{DIGEST}", "is not FROM"),
        (f"FROM caddy:2.11@sha256:{DIGEST}", "does not match"),
        (f"FROM caddy:latest@sha256:{DIGEST}", "does not match"),
    ],
    ids=["no-digest", "arg", "platform", "indented-lowercase", "two-part-tag", "latest"],
)
def test_docker_versions_refuses_a_caddy_line_it_cannot_read_strictly(line: str, message: str):
    dockerfile = DOCKERFILE.replace(f"FROM caddy:2.11.7@sha256:{DIGEST} AS caddy", line)

    with pytest.raises(PinError, match=message):
        hook.docker_versions(dockerfile)


def test_docker_versions_refuses_a_missing_or_repeated_image():
    without = DOCKERFILE.replace(f"FROM caddy:2.11.7@sha256:{DIGEST} AS caddy\n", "")
    twice = DOCKERFILE + f"FROM caddy:2.11.7@sha256:{DIGEST} AS caddy2\n"

    with pytest.raises(PinError, match="expected one FROM caddy, found 0"):
        hook.docker_versions(without)
    with pytest.raises(PinError, match="expected one FROM caddy, found 2"):
        hook.docker_versions(twice)


def test_docker_versions_refuses_a_node_tag_without_its_alpine_suffix():
    dockerfile = DOCKERFILE.replace("26.11.0-alpine", "26.11.0")

    with pytest.raises(PinError, match="node:26.11.0 does not match"):
        hook.docker_versions(dockerfile)


# pnpm_version


@pytest.mark.parametrize(
    ("manager", "version"),
    [("pnpm@12.9.0", "12.9.0"), ("pnpm@12.9.0+sha512.abc123", "12.9.0")],
)
def test_pnpm_version_reads_package_manager(manager: str, version: str):
    assert hook.pnpm_version(f'{{"packageManager": "{manager}"}}') == version


@pytest.mark.parametrize(
    "package_json",
    ["{}", '{"packageManager": "npm@11.0.0"}', '{"packageManager": "pnpm@12"}', "[]", "{"],
    ids=["missing", "npm", "partial", "array", "invalid"],
)
def test_pnpm_version_refuses_anything_but_exact_pnpm(package_json: str):
    with pytest.raises(PinError, match="web/package.json"):
        hook.pnpm_version(package_json)


# uv_build_requirement


@pytest.mark.parametrize(
    ("uv", "requirement"),
    [("0.12.23", "uv_build>=0.12.23,<0.13.0"), ("1.9.0", "uv_build>=1.9.0,<1.10.0")],
)
def test_uv_build_requirement_allows_up_to_the_next_minor(uv: str, requirement: str):
    assert hook.uv_build_requirement(uv) == requirement


# sync_mise


def test_sync_mise_sets_each_version_and_keeps_everything_else():
    wanted = {"caddy": "2.11.7", "pnpm": "12.9.0"}

    text = hook.sync_mise(MISE, wanted)

    assert text == MISE.replace('"2.11.6"', '"2.11.7"').replace('"12.8.2"', '"12.9.0"')


def test_sync_mise_with_versions_already_set_is_unchanged():
    assert hook.sync_mise(MISE, {"caddy": "2.11.6", "uv": "0.12.23"}) == MISE


def test_sync_mise_refuses_a_tool_missing_from_tools():
    with pytest.raises(PinError, match="no ruff version string"):
        hook.sync_mise(MISE, {"ruff": "1.0.0"})


def test_sync_mise_refuses_a_tool_line_it_cannot_find_once():
    two_tables = MISE + '\n[env]\ncaddy = "2.11.6"\n'
    inline = MISE.replace('caddy = "2.11.6"\n', "").replace(
        "[tools]", '[tools]\n"caddy" = "2.11.6"'
    )

    with pytest.raises(PinError, match="found 2"):
        hook.sync_mise(two_tables, {"caddy": "2.11.7"})
    with pytest.raises(PinError, match="found 0"):
        hook.sync_mise(inline, {"caddy": "2.11.7"})


def test_sync_mise_refuses_an_edit_that_lands_outside_the_tools_table():
    # The only caddy line is in [env]; [tools] gets it from a dotted key the edit cannot see
    text = '[tools.caddy]\nversion = "2.11.6"\n[env]\ncaddy = "2.11.6"\n'
    shadowed = '[env]\ncaddy = "2.11.6"\n[tools]\nnode = "26.10.0"\n'

    with pytest.raises(PinError, match="no caddy version string"):
        hook.sync_mise(text, {"caddy": "2.11.7"})
    with pytest.raises(PinError, match="no caddy version string"):
        hook.sync_mise(shadowed, {"caddy": "2.11.7"})


def test_sync_mise_refuses_an_edit_that_changes_another_table():
    # [tools] spells the key quoted, so the only bare node line the edit finds is [env]'s
    text = '[tools]\n"node" = "26.10.0"\n[env]\nnode = "26.10.0"\n'

    with pytest.raises(PinError, match="changed more than the pins it set"):
        hook.sync_mise(text, {"node": "26.11.0"})


def test_sync_mise_refuses_invalid_toml():
    with pytest.raises(PinError, match="mise.toml"):
        hook.sync_mise("[tools\n", {"caddy": "2.11.7"})


# sync_pyproject


def test_sync_pyproject_replaces_only_the_uv_build_requirement():
    text = hook.sync_pyproject(PYPROJECT, "uv_build>=0.13.1,<0.14.0")

    assert text == PYPROJECT.replace("uv_build>=0.12.23,<0.13.0", "uv_build>=0.13.1,<0.14.0")


def test_sync_pyproject_refuses_a_missing_or_repeated_uv_build():
    missing = PYPROJECT.replace('"uv_build>=0.12.23,<0.13.0"', '"hatchling"')
    repeated = PYPROJECT.replace('requires = ["', 'requires = ["uv_build", "')

    with pytest.raises(PinError, match="found 0"):
        hook.sync_pyproject(missing, "uv_build>=0.13.1,<0.14.0")
    with pytest.raises(PinError, match="found 2"):
        hook.sync_pyproject(repeated, "uv_build>=0.13.1,<0.14.0")


def test_sync_pyproject_refuses_the_requirement_string_appearing_twice_in_the_text():
    text = PYPROJECT + '\n[tool.x]\nnote = "uv_build>=0.12.23,<0.13.0"\n'

    with pytest.raises(PinError, match="once, found 2"):
        hook.sync_pyproject(text, "uv_build>=0.13.1,<0.14.0")


# parse_lock


def test_parse_lock_reads_coordinates_and_skips_comments():
    jars = hook.parse_lock(HEAD_LOCK)

    assert [(j.sha256, j.path, j.coordinate) for j in jars] == [
        (OLD_SHA, OLD_JAR, "org.example:lib"),
        (KEPT_SHA, KEPT_JAR, "org.other:dep"),
    ]


@pytest.mark.parametrize(
    "line",
    [
        f"{OLD_SHA} org/example/lib/1.0.0/lib-1.0.1.jar",
        f"{OLD_SHA} org/example/lib/1.0.0/other-1.0.0.jar",
        f"{OLD_SHA[:-1]} {OLD_JAR}",
        f"{OLD_SHA.upper()} {OLD_JAR}",
        f"{OLD_SHA}  {OLD_JAR}",
        f"{OLD_SHA} org/../lib/1.0.0/lib-1.0.0.jar",
        f"{OLD_SHA} https://evil.test/lib/1.0.0/lib-1.0.0.jar",
        f"{OLD_SHA} lib/1.0.0/lib-1.0.0.jar",
    ],
    ids=[
        "version-mismatch",
        "artifact-mismatch",
        "short-hash",
        "uppercase-hash",
        "two-spaces",
        "dot-dot",
        "url",
        "no-group",
    ],
)
def test_parse_lock_refuses_a_malformed_line(line: str):
    with pytest.raises(PinError, match=r"ics-validate\.lock:2: not"):
        hook.parse_lock(f"# Jars\n{line}\n")


def test_parse_lock_refuses_an_empty_lock():
    with pytest.raises(PinError, match="no jars"):
        hook.parse_lock("# Jars\n\n")


def test_parse_lock_refuses_two_versions_of_one_artifact():
    text = HEAD_LOCK + f"{'3' * 64} {NEW_JAR}\n"

    with pytest.raises(PinError, match="duplicate coordinate org.example:lib"):
        hook.parse_lock(text)


# refresh_lock


def test_refresh_lock_rehashes_only_the_bumped_jar():
    maven = maven_with(NEW_JAR, JAR)

    text = hook.refresh_lock(BUMPED_LOCK, HEAD_LOCK, maven)

    sha256 = hashlib.sha256(JAR).hexdigest()
    assert text == f"# Jars\n{sha256} {NEW_JAR}\n{KEPT_SHA} {KEPT_JAR}\n"
    assert maven.fetched == [hook.MAVEN + NEW_JAR, hook.MAVEN + NEW_JAR + ".sha1"]


def test_refresh_lock_of_an_unchanged_lock_fetches_nothing():
    maven = Maven({})

    assert hook.refresh_lock(HEAD_LOCK, HEAD_LOCK, maven) == HEAD_LOCK
    assert maven.fetched == []


def test_refresh_lock_refuses_a_changed_hash_for_an_unchanged_path():
    tampered = HEAD_LOCK.replace(KEPT_SHA, "f" * 64)

    with pytest.raises(PinError, match="dep-2.0.0.jar has a different hash from HEAD's"):
        hook.refresh_lock(tampered, HEAD_LOCK, Maven({}))


@pytest.mark.parametrize(
    "published",
    [
        hashlib.sha1(JAR).hexdigest().upper().encode(),
        f"{hashlib.sha1(JAR).hexdigest()}  lib-1.1.0.jar\n".encode(),
    ],
    ids=["uppercase", "with-filename"],
)
def test_refresh_lock_accepts_each_sha1_file_format(published: bytes):
    text = hook.refresh_lock(BUMPED_LOCK, HEAD_LOCK, maven_with(NEW_JAR, JAR, published))

    assert hashlib.sha256(JAR).hexdigest() in text


@pytest.mark.parametrize(
    ("published", "message"),
    [
        (hashlib.sha1(b"other").hexdigest().encode(), "does not match its published SHA-1"),
        (b"", "is not a SHA-1"),
        (b"<html>not found</html>", "is not a SHA-1"),
    ],
    ids=["mismatch", "empty", "html"],
)
def test_refresh_lock_refuses_a_jar_its_sha1_file_does_not_vouch_for(
    published: bytes, message: str
):
    with pytest.raises(PinError, match=message):
        hook.refresh_lock(BUMPED_LOCK, HEAD_LOCK, maven_with(NEW_JAR, JAR, published))


def test_refresh_lock_fails_when_the_jar_cannot_be_fetched():
    with pytest.raises(PinError, match="404"):
        hook.refresh_lock(BUMPED_LOCK, HEAD_LOCK, Maven({}))


# check and upgrade


def test_check_reports_each_copy_out_of_step(tmp_path: Path):
    root = write_repo(tmp_path)

    drift = "is out of step with the Dockerfile or web/package.json; run bin/renovate-postupgrade"
    assert hook.check(root) == [f"mise.toml {drift}", f"pyproject.toml {drift}"]


def test_upgrade_brings_every_copy_into_step_and_check_then_passes(tmp_path: Path):
    root = write_repo(tmp_path, lock=BUMPED_LOCK)

    written = hook.upgrade(root, HEAD_LOCK, maven_with(NEW_JAR, JAR))

    assert written == ["mise.toml", "pyproject.toml", "tools/ics-validate.lock"]
    mise = (root / "mise.toml").read_text()
    assert 'caddy = "2.11.7"' in mise
    assert 'node = "26.11.0"' in mise
    assert 'pnpm = "12.9.0"  # pinned' in mise
    assert 'uv = "0.13.1"' in mise
    assert 'python = "3.14.8"' in mise
    assert '"uv_build>=0.13.1,<0.14.0"' in (root / "pyproject.toml").read_text()
    assert hook.check(root) == []


def test_upgrade_twice_rewrites_nothing_the_second_time(tmp_path: Path):
    root = write_repo(tmp_path)
    hook.upgrade(root, HEAD_LOCK, Maven({}))

    assert hook.upgrade(root, HEAD_LOCK, Maven({})) == []


def test_upgrade_that_fails_on_the_lock_writes_nothing(tmp_path: Path):
    root = write_repo(tmp_path, lock=BUMPED_LOCK)

    with pytest.raises(PinError, match="404"):
        hook.upgrade(root, HEAD_LOCK, Maven({}))
    assert (root / "mise.toml").read_text() == MISE
    assert (root / "pyproject.toml").read_text() == PYPROJECT
    assert (root / "tools/ics-validate.lock").read_text() == BUMPED_LOCK


def test_check_reports_a_malformed_lock(tmp_path: Path):
    root = write_repo(tmp_path, lock="# nothing\n")

    assert hook.check(root) == ["tools/ics-validate.lock: no jars"]


# http_fetch


class Server:
    """A loopback HTTP server: redirects maps a path to its Location, other paths return body."""

    def __init__(self) -> None:
        self.body = b""
        self.redirects: dict[str, str] = {}
        self.paths: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                server.paths.append(self.path)
                if (location := server.redirects.get(self.path)) is not None:
                    self.send_response(302)
                    self.send_header("Location", location)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(server.body)))
                self.end_headers()
                self.wfile.write(server.body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.httpd.server_port}"
        self.url = f"{self.origin}/x"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    """A loopback server, with http allowed on its own origin and https required elsewhere."""
    s = Server()
    thread = threading.Thread(target=s.httpd.serve_forever, daemon=True)
    thread.start()
    refused = hook._redirect_refused
    monkeypatch.setattr(
        hook,
        "_redirect_refused",
        lambda url: None if url.startswith(f"{s.origin}/") else refused(url),
    )
    yield s
    s.httpd.shutdown()
    s.httpd.server_close()


def test_http_fetch_returns_the_body(server: Server):
    server.body = b"x" * 10

    assert hook.http_fetch(server.url, 10) == b"x" * 10


def test_http_fetch_refuses_a_body_over_the_limit(server: Server):
    server.body = b"x" * 11

    with pytest.raises(PinError, match="larger than 10 bytes"):
        hook.http_fetch(server.url, 10)


def test_http_fetch_refuses_a_redirect_to_http(server: Server):
    server.redirects["/x"] = "http://127.0.0.1:1/elsewhere"

    with pytest.raises(PinError, match="redirect refused: http scheme"):
        hook.http_fetch(server.url, 10)


def test_http_fetch_refuses_a_redirect_loop_between_two_urls(server: Server):
    server.redirects = {"/x": f"{server.origin}/a", "/a": f"{server.origin}/b"}
    server.redirects["/b"] = f"{server.origin}/a"

    with pytest.raises(PinError, match="HTTP 302"):
        hook.http_fetch(server.url, 10)
    assert server.paths == ["/x", "/a", "/b"]


@pytest.mark.parametrize("url", ["http://repo1.maven.org/x", "file:///etc/passwd", "https://"])
def test_http_fetch_refuses_a_url_that_is_not_https(url: str):
    with pytest.raises(PinError, match="not an HTTPS URL"):
        hook.http_fetch(url, 10)


# The script itself


def test_script_rejects_unknown_arguments():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--fix"], capture_output=True, text=True, check=False
    )

    assert result.returncode == 2
    assert "Usage" in result.stderr


def test_script_check_exits_1_on_drift_and_0_once_in_step(tmp_path: Path):
    (tmp_path / "bin").mkdir()
    script = shutil.copy(SCRIPT, tmp_path / "bin" / "renovate-postupgrade")
    write_repo(tmp_path)

    def run() -> subprocess.CompletedProcess[str]:
        argv = [sys.executable, str(script), "--check"]
        return subprocess.run(argv, capture_output=True, text=True, check=False)

    drifted = run()
    hook.upgrade(tmp_path, HEAD_LOCK, Maven({}))
    in_step = run()

    assert drifted.returncode == 1
    assert "mise.toml is out of step" in drifted.stderr
    assert (in_step.returncode, in_step.stderr) == (0, "")
