"""Fetch layer. Injectable so that tests never touch the network."""

import http.client
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional

from . import config


class FetchError(Exception):
    """Any condition that means we did not get usable content."""


@dataclass
class FetchResult:
    url: str
    status: int  # 200 or 304
    body: Optional[str]  # None iff status == 304
    etag: Optional[str] = None
    last_modified: Optional[str] = None

    @property
    def not_modified(self) -> bool:
        return self.status == 304


class Fetcher:
    """Interface. `caching` maps url -> {'etag':..., 'last_modified':...}."""

    def fetch(self, url: str, caching: Optional[Dict[str, str]] = None) -> FetchResult:
        raise NotImplementedError


class HttpFetcher(Fetcher):
    """Real network fetcher: identifiable UA, conditional GET, rate limited."""

    def __init__(self, min_interval: float = config.MIN_REQUEST_INTERVAL,
                 timeout: float = config.HTTP_TIMEOUT, sleeper=time.sleep,
                 opener=urllib.request.urlopen,
                 user_agent: str = config.USER_AGENT):
        self.min_interval = min_interval
        self.timeout = timeout
        self.user_agent = user_agent
        self._sleep = sleeper
        # Injectable so the error-classification paths below can be tested
        # without a socket. Same reason the fetcher itself is injectable.
        self._open = opener
        self._last_request_at = None  # type: Optional[float]

    def _wait_turn(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            self._sleep(self.min_interval - elapsed)

    def fetch(self, url: str, caching: Optional[Dict[str, str]] = None) -> FetchResult:
        self._wait_turn()
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", self.user_agent)
        req.add_header("Accept", "text/html,application/xhtml+xml")
        caching = caching or {}
        if caching.get("etag"):
            req.add_header("If-None-Match", caching["etag"])
        if caching.get("last_modified"):
            req.add_header("If-Modified-Since", caching["last_modified"])

        try:
            with self._open(req, timeout=self.timeout) as resp:
                status = resp.getcode()
                raw = resp.read()
                headers = resp.headers
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return FetchResult(url=url, status=304, body=None,
                                   etag=caching.get("etag"),
                                   last_modified=caching.get("last_modified"))
            raise FetchError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise FetchError("network error for %s: %s" % (url, exc.reason))
        # Everything below is a real network failure that urllib does NOT wrap
        # in a URLError, because it is raised after the connection is already
        # open. Uncaught they escaped as a traceback and exit 1 -- "internal
        # error, read the traceback" -- for the most ordinary thing a network
        # does. They are fetch failures, and they exit 2 like every other one.
        except http.client.HTTPException as exc:
            # RemoteDisconnected lands here: the server accepted the connection
            # and then closed it without answering. It is also a
            # ConnectionResetError, so this clause must precede the OSError one.
            raise FetchError(
                "%s closed the connection without sending a complete "
                "response for %s (%s)"
                % ("the server", url, type(exc).__name__))
        except (socket.timeout, TimeoutError):
            raise FetchError("timed out after %gs waiting for %s"
                             % (self.timeout, url))
        except OSError as exc:
            raise FetchError("network error for %s: %s: %s"
                             % (url, type(exc).__name__, exc))
        finally:
            self._last_request_at = time.monotonic()

        if status != 200:
            raise FetchError("unexpected HTTP %s for %s" % (status, url))

        charset = headers.get_content_charset() or "utf-8"
        try:
            body = raw.decode(charset, errors="replace")
        except LookupError:
            body = raw.decode("utf-8", errors="replace")
        return FetchResult(url=url, status=200, body=body,
                           etag=headers.get("ETag"),
                           last_modified=headers.get("Last-Modified"))


class FixtureFetcher(Fetcher):
    """Replays recorded pages from disk. Used by every test. Never opens a socket.

    `mapping` is url -> path. A relative path is resolved against `base_dir`,
    which defaults to the working directory; the config loader hands over
    absolute paths, resolved against the config file's own directory, so a
    config file can name its fixtures relative to itself.
    """

    def __init__(self, mapping: Dict[str, str], base_dir: Optional[str] = None):
        self.mapping = dict(mapping)
        self.base_dir = base_dir or os.getcwd()
        self.calls = []  # type: list

    def fetch(self, url: str, caching: Optional[Dict[str, str]] = None) -> FetchResult:
        self.calls.append(url)
        try:
            name = self.mapping[url]
        except KeyError:
            raise FetchError("no fixture recorded for %s" % url)
        path = name if os.path.isabs(name) else os.path.join(self.base_dir, name)
        if not os.path.exists(path):
            raise FetchError("fixture file missing: %s" % path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            body = fh.read()
        return FetchResult(url=url, status=200, body=body,
                           etag='"fixture"', last_modified=None)


class StringFetcher(Fetcher):
    """Serves HTML straight from memory. For focused parser unit tests."""

    def __init__(self, mapping: Dict[str, str]):
        self.mapping = dict(mapping)

    def fetch(self, url: str, caching: Optional[Dict[str, str]] = None) -> FetchResult:
        if url not in self.mapping:
            raise FetchError("no canned body for %s" % url)
        return FetchResult(url=url, status=200, body=self.mapping[url])
