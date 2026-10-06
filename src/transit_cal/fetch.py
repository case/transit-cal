"""Download a feed's latest GTFS version from Transitland's REST API, reusing an unchanged one."""

import hashlib
import http
import http.client
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

API_URL = "https://transit.land"
REGIONAL_FEED = "f-sf~bay~area~rg"
# A Transitland feed Onestop ID: https://www.transit.land/documentation/onestop-id-scheme/
FEED_ID = re.compile(r"f(-[0-9a-z~]+){1,2}")
SHA1 = re.compile(r"[0-9a-f]{40}")
# ASCII digits only: str.isdigit also accepts other scripts' digits, and int() has a digit limit
CONTENT_LENGTH = re.compile(r"[0-9]{1,19}")
TIMEOUT_SECONDS = 60
CHUNK_BYTES = 1 << 20
# The 511 feed record is ~240 KB, mostly geometry
MAX_RECORD_BYTES = 4 * 1024 * 1024
# The 511 zip is ~59 MB; a far larger download is refused before it can fill the volume
MAX_ZIP_BYTES = 512 * 1024 * 1024


class FetchError(ValueError):
    """The feed could not be downloaded, or the download is not a whole zip."""


@dataclass(frozen=True, slots=True)
class Fetched:
    """The zip, whether an unchanged copy was reused, and why reuse was skipped, if it failed."""

    path: Path
    reused: bool
    warning: str | None


def fetch_latest(
    feed: str, out: Path, api_key: str, base_url: str = API_URL, timeout: float = TIMEOUT_SECONDS
) -> Fetched:
    """Make feed's latest GTFS zip available as out/<sha1>.zip.

    Transitland names a feed version by that SHA-1. One small feed-record query finds the latest
    SHA-1; a zip already in out whose bytes match it is reused. Otherwise, or if the record is
    unusable (reported in the warning), the zip is downloaded and named by its own SHA-1. The key
    goes only to base_url, never on a redirect, and redirects must stay on HTTPS.
    Raises FetchError for a malformed feed, a blank key, a failed, truncated or oversized download,
    or a body that is not a zip, and then leaves nothing new.
    """
    if not FEED_ID.fullmatch(feed):
        raise FetchError(f"feed {feed!r} is not a Transitland feed Onestop ID")
    if not api_key.strip():
        raise FetchError("TRANSITLAND_API_KEY is empty")
    record_url = f"{base_url}/api/v2/rest/feeds/{feed}"

    warning = None
    try:
        latest = _latest_sha1(_request(record_url, api_key), feed, timeout)
    except FetchError as e:
        latest, warning = None, f"{e}; downloading instead"
    if latest is not None:
        cached = out / f"{latest}.zip"
        if cached.is_file() and _sha1(cached) == latest:
            return Fetched(cached, reused=True, warning=None)

    request = _request(f"{record_url}/download_latest_feed_version", api_key)
    out.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=out, prefix=".fetch-", suffix=".zip")
    partial = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            sha1 = _download(request, f, feed, timeout)
        if not zipfile.is_zipfile(partial):
            raise FetchError(f"{feed}: the download is not a zip")
        return Fetched(partial.replace(out / f"{sha1}.zip"), reused=False, warning=warning)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    """Follow at most 3 redirects, each to HTTPS."""

    max_redirections = 3

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        # Closed unread: urllib would otherwise read a redirect's whole body, with no size limit
        fp.close()
        if (reason := _redirect_refused(newurl)) is not None:
            raise urllib.error.URLError(f"redirect refused: {reason}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _redirect_refused(url: str) -> str | None:
    """Why a redirect to url is refused, or None to follow it."""
    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port  # Raises ValueError for a malformed port
    except ValueError:
        return "malformed URL"
    if parts.scheme != "https":
        return f"{parts.scheme or 'no'} scheme, not https"
    if not parts.hostname:
        return "no host"
    return None


_OPENER = urllib.request.build_opener(_HttpsRedirects)


def _request(url: str, api_key: str) -> urllib.request.Request:
    request = urllib.request.Request(url)
    request.add_unredirected_header("apikey", api_key)
    return request


def _latest_sha1(request: urllib.request.Request, feed: str, timeout: float) -> str:
    """The SHA-1 of the feed's latest version, from its feed record. Raises FetchError."""
    what = f"{feed} feed record"
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read(MAX_RECORD_BYTES + 1)
    except urllib.error.HTTPError as e:
        e.close()
        raise FetchError(f"{what}: HTTP {e.code} {_phrase(e.code)}") from None
    # ValueError: urllib parses a server's redirect Location before any policy sees it
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        raise FetchError(f"{what}: {getattr(e, 'reason', e)}") from None
    if len(body) > MAX_RECORD_BYTES:
        raise FetchError(f"{what}: larger than {MAX_RECORD_BYTES} bytes")
    try:
        record = json.loads(body)
    except ValueError:
        raise FetchError(f"{what}: not JSON") from None
    try:
        sha1 = record["feeds"][0]["feed_state"]["feed_version"]["sha1"]
    except KeyError, IndexError, TypeError:
        sha1 = None
    if not isinstance(sha1, str) or not SHA1.fullmatch(sha1):
        raise FetchError(f"{what}: no feed version SHA-1")
    return sha1


def _sha1(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, lambda: hashlib.sha1(usedforsecurity=False)).hexdigest()


def _download(request: urllib.request.Request, f: IO[bytes], feed: str, timeout: float) -> str:
    """Stream the response into f; returns its SHA-1 hex digest."""
    digest = hashlib.sha1(usedforsecurity=False)
    received = 0
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            declared = _content_length(response.headers.get("Content-Length", ""))
            if declared is not None and declared > MAX_ZIP_BYTES:
                raise FetchError(f"could not download {feed}: {declared} bytes is over the limit")
            while chunk := response.read(CHUNK_BYTES):
                received += len(chunk)
                if received > MAX_ZIP_BYTES:
                    raise FetchError(f"could not download {feed}: over {MAX_ZIP_BYTES} bytes")
                digest.update(chunk)
                f.write(chunk)
    except FetchError:
        raise
    except urllib.error.HTTPError as e:
        e.close()
        # The phrase comes from the status code, never the server, which could echo the key.
        raise FetchError(f"could not download {feed}: HTTP {e.code} {_phrase(e.code)}") from None
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        raise FetchError(f"could not download {feed}: {getattr(e, 'reason', e)}") from None
    # http.client returns a short body without error when the connection closes early.
    if declared is not None and declared != received:
        raise FetchError(f"could not download {feed}: got {received} of {declared} bytes")
    return digest.hexdigest()


def _content_length(header: str) -> int | None:
    """The declared body size, or None when absent or malformed; the stream limit still applies."""
    return int(header) if CONTENT_LENGTH.fullmatch(header) else None


def _phrase(code: int) -> str:
    try:
        return http.HTTPStatus(code).phrase
    except ValueError:
        return "error"
