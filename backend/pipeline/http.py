"""Outbound HTTP for the pipeline: every page, image and robots.txt request goes through Fetcher.

Crawl policy
- Requests identify themselves with USER_AGENT, which carries a contact URL.
- robots.txt is checked before every request and cached per origin for
  ROBOTS_TTL seconds. A robots.txt answering 401 or 403, or one that cannot be
  parsed, disallows the whole host (cached for ROBOTS_ERROR_TTL seconds); any
  other 4xx allows it. A Crawl-delay above MAX_WAIT seconds disallows the host.
- A robots.txt answering 429 or 5xx, or not at all, fails the URL that needed
  it and pauses the host for DEFAULT_RETRY_AFTER seconds. Nothing is cached, so
  the first URL of the host after the pause asks for robots.txt again.
- Two requests to the same host are at least `min_delay` seconds apart (or the
  robots.txt Crawl-delay, when longer). A 429/503 answer pauses the host for its
  Retry-After. While a host is paused for longer than MAX_WAIT, its URLs raise
  RetryLater without being requested, instead of blocking the stage.

Network safety
- Only http and https URLs are fetched.
- The host must resolve to globally routable addresses only, and the address
  actually connected to is checked again, so neither a hostile DNS answer nor
  DNS rebinding reaches loopback, private or link-local networks.
- Redirects are followed by hand (at most MAX_REDIRECTS) and every hop is
  checked like the first URL, robots.txt included. The body of a redirect
  answer is never read.
- Bodies are streamed and capped. Reading one body stops after `deadline`
  seconds, checked after every socket read (each waits at most `timeout`).
- Proxies, .netrc and other environment settings are ignored.
"""
import ipaddress
import logging
import socket
import time
import urllib.robotparser
from collections import OrderedDict
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

log = logging.getLogger(__name__)

USER_AGENT = "CortexBot/1.0 (+https://github.com/piculjantechnologies/cortex)"
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_ROBOTS_BYTES = 500 * 1024
SNIFF_BYTES = 8192
MAX_REDIRECTS = 5
ROBOTS_TTL = 24 * 3600
ROBOTS_ERROR_TTL = 3600
ROBOTS_CACHE_SIZE = 10_000
MAX_WAIT = 30.0
DEFAULT_RETRY_AFTER = 60.0
MAX_RETRY_AFTER = 3600.0
READ_CHUNK = 64 * 1024


class FetchError(Exception):
    """The URL was not fetched; str(exc) is a short reason."""


class UnsafeURLError(FetchError):
    """The URL's scheme or address is not allowed."""


class RobotsDisallowed(FetchError):
    """robots.txt does not allow the URL."""


class RetryLater(FetchError):
    """The URL was not requested because its host is paused; fetch it again later."""


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    content: bytes


def _check_address(address):
    ip = ipaddress.ip_address(address)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise UnsafeURLError(f"address not allowed: {ip}")


def _check_host(host, port):
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise FetchError(f"cannot resolve {host}") from exc
    for *_, sockaddr in infos:
        _check_address(sockaddr[0])


def _validate(url):
    """Check scheme and host of `url`; return its urlsplit() parts."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise FetchError("invalid URL") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURLError(f"URL not allowed: {url[:200]}")
    _check_host(parts.hostname, port or (443 if parts.scheme == "https" else 80))
    return parts


class _PeerCheck:
    """Refuse a connection whose peer address is not global (DNS rebinding)."""

    def _new_conn(self):
        sock = super()._new_conn()
        try:
            _check_address(sock.getpeername()[0])
        except UnsafeURLError:
            sock.close()
            raise
        return sock


class _HTTPConnection(_PeerCheck, HTTPConnection):
    pass


class _HTTPSConnection(_PeerCheck, HTTPSConnection):
    pass


class _HTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _HTTPConnection


class _HTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _HTTPSConnection


class _Session(requests.Session):
    def get_redirect_target(self, resp):
        # Fetcher follows redirects itself. Returning None also keeps
        # Session.send (even with allow_redirects=False) from reading the whole
        # redirect body, uncapped, and from parsing its Location header.
        return None


class _GlobalOnlyAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _HTTPConnectionPool,
            "https": _HTTPSConnectionPool,
        }


def _retry_after(value):
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP date)."""
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(value).timestamp() - time.time()
            except (TypeError, ValueError):
                seconds = DEFAULT_RETRY_AFTER
    else:
        seconds = DEFAULT_RETRY_AFTER
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


class Fetcher:
    """One requests.Session plus the robots.txt cache and per-host throttle."""

    def __init__(self, user_agent=USER_AGENT, min_delay=1.0, timeout=5, deadline=30.0,
                 sleep=time.sleep, clock=time.monotonic):
        self.user_agent = user_agent
        self.min_delay = min_delay
        self.timeout = timeout
        self.deadline = deadline  # seconds allowed for reading one body
        self._sleep = sleep
        self._clock = clock
        self._robots = OrderedDict()  # origin -> (expires, parser, crawl_delay or None)
        self._next_request = {}  # hostname -> earliest clock() of the next request
        self.session = _Session()
        self.session.trust_env = False
        self.session.headers["User-Agent"] = user_agent
        adapter = _GlobalOnlyAdapter()
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def fetch(self, url, max_bytes=MAX_IMAGE_BYTES, truncate=False):
        """GET `url` under the crawl policy and return its body.

        A body longer than `max_bytes` raises FetchError, or is cut to
        `max_bytes` when `truncate` is true (for sniffing a file type).
        Raises FetchError (or a subclass) for anything but a 2xx answer; the
        subclass RetryLater means the URL was not requested and may be
        fetched again later.
        """
        url, response = self._open(url, check_robots=True)
        with response:
            if not 200 <= response.status_code < 300:
                raise FetchError(f"HTTP {response.status_code}")
            return FetchResult(url, self._read(response, max_bytes, truncate))

    def _open(self, url, check_robots):
        for _ in range(MAX_REDIRECTS + 1):
            parts = _validate(url)
            delay = self.min_delay
            if check_robots:
                delay = max(delay, self._check_robots(parts, url) or 0)
            response = self._get(url, parts.hostname, delay)
            if not response.is_redirect:
                return url, response
            response.close()
            try:
                url = urljoin(url, response.headers["Location"])
            except ValueError as exc:
                raise FetchError("invalid redirect") from exc
        raise FetchError("too many redirects")

    def _get(self, url, host, delay):
        self._throttle(host, delay)
        try:
            response = self.session.get(url, stream=True, allow_redirects=False, timeout=self.timeout)
        except requests.RequestException as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}"[:300]) from exc
        if response.status_code in (429, 503):
            self._pause(host, _retry_after(response.headers.get("Retry-After")))
        return response

    def _read(self, response, max_bytes, truncate):
        length = response.headers.get("Content-Length", "")
        if not truncate and length.isdigit() and int(length) > max_bytes:
            raise FetchError("too large")
        # Without truncate, one byte past max_bytes tells a body that is too
        # large from one of exactly max_bytes.
        limit = max_bytes if truncate else max_bytes + 1
        started = self._clock()
        body = bytearray()
        try:
            while len(body) < limit:
                # read1 returns as soon as some data has arrived (waiting at
                # most `timeout`), so the deadline is checked after every read.
                chunk = response.raw.read1(min(READ_CHUNK, limit - len(body)), decode_content=True)
                if not chunk:
                    break
                body += chunk
                if self._clock() - started > self.deadline:
                    raise FetchError("read took too long")
        except urllib3.exceptions.HTTPError as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}"[:300]) from exc
        if len(body) > max_bytes:
            raise FetchError("too large")
        return bytes(body)

    def _throttle(self, host, delay):
        """Wait until `host` may be requested again, then book the next slot."""
        now = self._clock()
        wait = self._next_request.get(host, now) - now
        if wait > MAX_WAIT:
            raise RetryLater(f"{host} is paused; retry later")
        if wait > 0:
            self._sleep(wait)
            now += wait
        if len(self._next_request) > ROBOTS_CACHE_SIZE:
            self._next_request = {h: t for h, t in self._next_request.items() if t > now}
        self._next_request[host] = now + delay

    def _pause(self, host, seconds):
        until = self._clock() + seconds
        self._next_request[host] = max(self._next_request.get(host, until), until)

    def _check_robots(self, parts, url):
        """Raise RobotsDisallowed unless robots.txt allows `url`; return its Crawl-delay."""
        parser, crawl_delay = self._robots_for(parts)
        if not parser.can_fetch(self.user_agent, url):
            raise RobotsDisallowed("disallowed by robots.txt")
        if crawl_delay and crawl_delay > MAX_WAIT:
            raise RobotsDisallowed(f"robots.txt crawl-delay {crawl_delay:g}s")
        return crawl_delay

    def _robots_for(self, parts):
        origin = f"{parts.scheme}://{parts.netloc}"
        now = self._clock()
        entry = self._robots.get(origin)
        if entry and entry[0] > now:
            self._robots.move_to_end(origin)
            return entry[1], entry[2]
        parser = urllib.robotparser.RobotFileParser(origin + "/robots.txt")
        ttl = ROBOTS_TTL
        try:
            _, response = self._open(origin + "/robots.txt", check_robots=False)
            with response:
                status = response.status_code
                if status == 429 or status >= 500:
                    raise FetchError(f"HTTP {status}")
                if status == 200:
                    body = self._read(response, MAX_ROBOTS_BYTES, truncate=True)
                    try:
                        parser.parse(body.decode("utf-8", "replace").splitlines())
                    except ValueError:  # e.g. "Crawl-delay: ²" passes isdigit() but not int()
                        log.info("robots.txt for %s cannot be parsed; treating the host as disallowed", origin)
                        parser = urllib.robotparser.RobotFileParser(origin + "/robots.txt")
                        parser.disallow_all = True
                        ttl = ROBOTS_ERROR_TTL
                elif status in (401, 403):
                    parser.disallow_all = True
                    ttl = ROBOTS_ERROR_TTL
                else:
                    parser.allow_all = True
        except (UnsafeURLError, RetryLater):
            raise
        except FetchError as exc:
            # This URL fails; the host's other URLs wait out the pause
            # (RetryLater) instead of failing without being requested.
            log.info("robots.txt for %s unavailable (%s); pausing the host", origin, exc)
            self._pause(parts.hostname, DEFAULT_RETRY_AFTER)
            raise FetchError(f"robots.txt unavailable ({exc})"[:300]) from exc
        crawl_delay = parser.crawl_delay(self.user_agent)
        crawl_delay = float(crawl_delay) if crawl_delay is not None else None
        self._robots[origin] = (now + ttl, parser, crawl_delay)
        self._robots.move_to_end(origin)
        while len(self._robots) > ROBOTS_CACHE_SIZE:
            self._robots.popitem(last=False)
        return parser, crawl_delay


_default = None


def get_fetcher():
    """The process-wide Fetcher used by the pipeline stages."""
    global _default
    if _default is None:
        _default = Fetcher()
    return _default
