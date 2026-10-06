import hashlib
import io
import json
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from transit_cal import fetch as fetch_module
from transit_cal.fetch import Fetched, FetchError, fetch_latest

KEY = "test-key-not-real"
FEED = "f-test~feed"
META_PATH = f"/api/v2/rest/feeds/{FEED}"
PATH = f"{META_PATH}/download_latest_feed_version"


def zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("agency.txt", "agency_id,agency_name\nA,Agency A\n")
    return buf.getvalue()


ZIP = zip_bytes()
SHA1 = hashlib.sha1(ZIP).hexdigest()


def metadata(sha1: object) -> bytes:
    """Transitland's feed record, reduced to the field fetch reads."""
    return json.dumps({"feeds": [{"feed_state": {"feed_version": {"sha1": sha1}}}]}).encode()


class Server:
    """A loopback Transitland: tests set its record and download responses; it logs requests."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.meta: Callable[[BaseHTTPRequestHandler], None] = lambda h: send(h, 200, metadata(SHA1))
        self.respond: Callable[[BaseHTTPRequestHandler], None] = lambda h: send(h, 200, ZIP)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                (outer.meta if self.path == META_PATH else outer.respond)(self)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"


def send(handler: BaseHTTPRequestHandler, status: int, body: bytes, **headers: str) -> None:
    handler.send_response(status)
    for name, value in headers.items():
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


@pytest.fixture
def server() -> Iterator[Server]:
    yield from serve()


@pytest.fixture
def other() -> Iterator[Server]:
    yield from serve()


def serve() -> Iterator[Server]:
    s = Server()
    thread = threading.Thread(target=s.httpd.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    yield s
    s.httpd.shutdown()
    s.httpd.server_close()
    thread.join()


@pytest.fixture
def loopback_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test servers are plain HTTP on loopback, which the real redirect policy refuses."""
    monkeypatch.setattr(fetch_module, "_redirect_refused", lambda url: None)


def fetch(server: Server, out: Path, *, feed: str = FEED, api_key: str = KEY) -> Path:
    return fetch_latest(feed=feed, out=out, api_key=api_key, base_url=server.url).path


def test_fetch_saves_the_zip_under_its_sha1_and_sends_the_key(server: Server, tmp_path: Path):
    path = fetch(server, tmp_path / "gtfs")

    assert path == tmp_path / "gtfs" / f"{SHA1}.zip"
    assert path.read_bytes() == ZIP
    assert [requested for requested, _ in server.requests] == [META_PATH, PATH]
    assert all(headers["apikey"] == KEY for _, headers in server.requests)


def test_fetch_of_an_unchanged_version_reuses_the_zip_without_downloading(
    server: Server, tmp_path: Path
):
    first = fetch_latest(feed=FEED, out=tmp_path, api_key=KEY, base_url=server.url)
    server.requests.clear()

    second = fetch_latest(feed=FEED, out=tmp_path, api_key=KEY, base_url=server.url)

    assert first == Fetched(tmp_path / f"{SHA1}.zip", reused=False, warning=None)
    assert second == Fetched(tmp_path / f"{SHA1}.zip", reused=True, warning=None)
    assert [requested for requested, _ in server.requests] == [META_PATH]
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{SHA1}.zip"]


def test_fetch_replaces_a_cached_zip_whose_bytes_do_not_match_its_name(
    server: Server, tmp_path: Path
):
    (tmp_path / f"{SHA1}.zip").write_bytes(b"corrupt")

    result = fetch_latest(feed=FEED, out=tmp_path, api_key=KEY, base_url=server.url)

    assert result.reused is False
    assert result.path.read_bytes() == ZIP
    assert [requested for requested, _ in server.requests] == [META_PATH, PATH]


def test_fetch_names_the_zip_by_its_own_sha1_when_a_new_version_lands_mid_fetch(
    server: Server, tmp_path: Path
):
    server.meta = lambda h: send(h, 200, metadata("0" * 40))

    result = fetch_latest(feed=FEED, out=tmp_path, api_key=KEY, base_url=server.url)

    assert result == Fetched(tmp_path / f"{SHA1}.zip", reused=False, warning=None)


@pytest.mark.parametrize(
    ("respond", "why"),
    [
        (lambda h: send(h, 500, b"down"), "HTTP 500"),
        (lambda h: send(h, 200, b"not json"), "not JSON"),
        (lambda h: send(h, 200, b'{"feeds": []}'), "no feed version SHA-1"),
        (lambda h: send(h, 200, metadata(None)), "no feed version SHA-1"),
        (lambda h: send(h, 200, metadata("ABC")), "no feed version SHA-1"),
        (lambda h: send(h, 200, metadata("../" + "a" * 37)), "no feed version SHA-1"),
        (lambda h: send(h, 200, b" " * (4 * 1024 * 1024 + 1)), "larger than"),
    ],
)
def test_fetch_falls_back_to_downloading_when_the_feed_record_is_unusable(
    server: Server, tmp_path: Path, respond: Callable[[BaseHTTPRequestHandler], None], why: str
):
    (tmp_path / f"{SHA1}.zip").write_bytes(ZIP)
    server.meta = respond

    result = fetch_latest(feed=FEED, out=tmp_path, api_key=KEY, base_url=server.url)

    assert result.path.read_bytes() == ZIP
    assert result.reused is False
    assert result.warning is not None and why in result.warning
    assert KEY not in result.warning
    assert [requested for requested, _ in server.requests] == [META_PATH, PATH]


@pytest.mark.usefixtures("loopback_redirects")
def test_feed_record_redirect_never_carries_the_key(server: Server, other: Server, tmp_path: Path):
    server.meta = lambda h: send(h, 302, b"", Location=f"{other.url}{META_PATH}")

    result = fetch_latest(feed=FEED, out=tmp_path, api_key=KEY, base_url=server.url)

    assert result.warning is None
    [(_, headers)] = other.requests
    assert "apikey" not in headers


@pytest.mark.usefixtures("loopback_redirects")
def test_fetch_follows_a_redirect_without_sending_the_key_to_the_other_host(
    server: Server, other: Server, tmp_path: Path
):
    server.respond = lambda h: send(h, 302, b"", Location=f"{other.url}/blob/{SHA1}.zip")

    path = fetch(server, tmp_path)

    assert path.read_bytes() == ZIP
    [(_, headers)] = other.requests
    assert "apikey" not in headers


@pytest.mark.parametrize(
    ("url", "refused"),
    [
        ("https://93.184.216.34/blob.zip", None),
        ("HTTPS://storage.test/blob.zip", None),
        ("http://93.184.216.34/blob.zip", "http scheme, not https"),
        ("ftp://93.184.216.34/blob.zip", "ftp scheme, not https"),
        ("https:///blob.zip", "no host"),
        ("https://[::1/blob.zip", "malformed URL"),
        ("https://93.184.216.34:99999/", "malformed URL"),
    ],
)
def test_redirect_policy_allows_only_well_formed_https(url: str, refused: str | None):
    reason = fetch_module._redirect_refused(url)

    if refused is None:
        assert reason is None
    else:
        assert reason is not None and refused in reason


@pytest.mark.parametrize("location", ["https://[::1/blob.zip", "https://h.test:99999/blob.zip"])
def test_fetch_turns_a_malformed_redirect_into_a_fetch_error(
    server: Server, tmp_path: Path, location: str
):
    server.respond = lambda h: send(h, 302, b"", Location=location)

    with pytest.raises(FetchError, match="could not download"):
        fetch(server, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_feed_record_with_a_malformed_redirect_falls_back_to_downloading(
    server: Server, tmp_path: Path
):
    server.meta = lambda h: send(h, 302, b"", Location="https://[::1/record")

    result = fetch_latest(feed=FEED, out=tmp_path, api_key=KEY, base_url=server.url)

    assert result.path.read_bytes() == ZIP
    assert result.warning is not None


@pytest.mark.parametrize("declared", ["\u00b9\u00b2", "9" * 5000, "12abc", "-1"])
def test_a_malformed_content_length_counts_as_undeclared(
    server: Server, tmp_path: Path, declared: str
):
    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", declared)
        handler.end_headers()
        handler.wfile.write(ZIP)

    server.respond = respond

    assert fetch(server, tmp_path).read_bytes() == ZIP


def test_fetch_refuses_a_redirect_to_plain_http_on_loopback(
    server: Server, other: Server, tmp_path: Path
):
    server.respond = lambda h: send(h, 302, b"", Location=f"{other.url}/blob/{SHA1}.zip")

    with pytest.raises(FetchError, match="redirect refused: http scheme"):
        fetch(server, tmp_path)
    assert other.requests == []
    assert list(tmp_path.iterdir()) == []


def test_feed_record_redirect_refused_falls_back_to_downloading(
    server: Server, other: Server, tmp_path: Path
):
    server.meta = lambda h: send(h, 302, b"", Location=f"{other.url}{META_PATH}")

    result = fetch_latest(feed=FEED, out=tmp_path, api_key=KEY, base_url=server.url)

    assert result.path.read_bytes() == ZIP
    assert result.warning is not None and "redirect refused" in result.warning
    assert other.requests == []


def hops(server: Server, count: int) -> Callable[[BaseHTTPRequestHandler], None]:
    """Redirect count times, through /hop/1 to /hop/<count>, then serve the zip."""

    def respond(handler: BaseHTTPRequestHandler) -> None:
        done = int(handler.path.rsplit("/", 1)[1]) if handler.path.startswith("/hop/") else 0
        if done < count:
            send(handler, 302, b"", Location=f"{server.url}/hop/{done + 1}")
        else:
            send(handler, 200, ZIP)

    return respond


@pytest.mark.usefixtures("loopback_redirects")
def test_fetch_follows_three_redirects(server: Server, tmp_path: Path):
    server.respond = hops(server, 3)

    assert fetch(server, tmp_path).read_bytes() == ZIP


@pytest.mark.usefixtures("loopback_redirects")
def test_fetch_refuses_a_fourth_redirect(server: Server, tmp_path: Path):
    server.respond = hops(server, 4)

    with pytest.raises(FetchError, match="HTTP 302"):
        fetch(server, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.usefixtures("loopback_redirects")
def test_fetch_never_reads_the_body_of_a_redirect(server: Server, tmp_path: Path):
    def endless(handler: BaseHTTPRequestHandler) -> None:
        if handler.path.startswith("/blob"):
            send(handler, 200, ZIP)
            return
        handler.send_response(302)
        handler.send_header("Location", f"{server.url}/blob")
        handler.send_header("Content-Length", str(10**9))
        handler.end_headers()
        handler.wfile.write(b"x" * 1024)
        handler.wfile.flush()
        time.sleep(3)

    server.respond = endless
    started = time.monotonic()

    path = fetch_latest(feed=FEED, out=tmp_path, api_key=KEY, base_url=server.url, timeout=10).path

    assert path.read_bytes() == ZIP
    assert time.monotonic() - started < 2


def test_fetch_refuses_a_declared_size_over_the_limit_before_reading(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(fetch_module, "MAX_ZIP_BYTES", len(ZIP) - 1)

    with pytest.raises(FetchError, match=f"{len(ZIP)} bytes is over the limit"):
        fetch(server, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_fetch_stops_an_undeclared_body_once_it_passes_the_limit(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def unsized(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.end_headers()
        handler.wfile.write(ZIP)

    monkeypatch.setattr(fetch_module, "MAX_ZIP_BYTES", len(ZIP) - 1)
    server.respond = unsized

    with pytest.raises(FetchError, match=f"over {len(ZIP) - 1} bytes"):
        fetch(server, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_fetch_accepts_a_zip_exactly_at_the_limit(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(fetch_module, "MAX_ZIP_BYTES", len(ZIP))

    assert fetch(server, tmp_path).read_bytes() == ZIP


@pytest.mark.parametrize("status", [401, 404, 429, 500])
def test_fetch_http_error_raises_with_status_and_never_the_key(
    server: Server, tmp_path: Path, status: int
):
    server.respond = lambda h: send(h, status, b"nope")

    with pytest.raises(FetchError, match=f"HTTP {status}") as e:
        fetch(server, tmp_path)
    assert KEY not in str(e.value)
    assert list(tmp_path.iterdir()) == []


def test_fetch_non_zip_body_raises_and_leaves_no_file(server: Server, tmp_path: Path):
    server.respond = lambda h: send(h, 200, b"<html>not a zip</html>")

    with pytest.raises(FetchError, match="not a zip"):
        fetch(server, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_fetch_truncated_body_raises_and_leaves_no_file(server: Server, tmp_path: Path):
    def truncate(h: BaseHTTPRequestHandler) -> None:
        h.send_response(200)
        h.send_header("Content-Length", str(len(ZIP) + 100))
        h.end_headers()
        h.wfile.write(ZIP)
        h.close_connection = True

    server.respond = truncate

    with pytest.raises(FetchError):
        fetch(server, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_fetch_unreachable_host_raises(tmp_path: Path):
    s = Server()
    url = s.url
    s.httpd.server_close()

    with pytest.raises(FetchError, match="could not download"):
        fetch_latest(feed=FEED, out=tmp_path, api_key=KEY, base_url=url)


@pytest.mark.parametrize("api_key", ["", " "])
def test_fetch_without_a_key_raises_before_any_request(
    server: Server, tmp_path: Path, api_key: str
):
    with pytest.raises(FetchError, match="TRANSITLAND_API_KEY"):
        fetch(server, tmp_path, api_key=api_key)
    assert server.requests == []


@pytest.mark.parametrize("feed", ["", "o-test", "f-../x", "f-a/b", "F-Test", "f-a?b=c"])
def test_fetch_rejects_a_feed_id_not_in_transitland_form(server: Server, tmp_path: Path, feed: str):
    with pytest.raises(FetchError, match="feed"):
        fetch(server, tmp_path, feed=feed)
    assert server.requests == []


def test_fetch_http_error_never_repeats_the_server_reason(server: Server, tmp_path: Path):
    def echo(h: BaseHTTPRequestHandler) -> None:
        h.send_response(401, f"bad key {KEY}")
        h.send_header("Content-Length", "0")
        h.end_headers()

    server.respond = echo

    with pytest.raises(FetchError, match="HTTP 401 Unauthorized") as e:
        fetch(server, tmp_path)
    assert KEY not in str(e.value)


def test_fetch_stalled_body_times_out_and_leaves_no_file(server: Server, tmp_path: Path):
    def stall(h: BaseHTTPRequestHandler) -> None:
        h.send_response(200)
        h.send_header("Content-Length", str(len(ZIP)))
        h.end_headers()
        h.wfile.flush()
        time.sleep(0.5)

    server.respond = stall

    with pytest.raises(FetchError, match="timed out"):
        fetch_latest(feed=FEED, out=tmp_path, api_key=KEY, base_url=server.url, timeout=0.1)
    assert list(tmp_path.iterdir()) == []
