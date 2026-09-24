"""pipeline.http: SSRF checks, robots.txt, User-Agent, throttling, size caps and read deadline."""
import gzip
import http.server
import io
import threading
import time

import pytest
import requests
import urllib3
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from pipeline import http as pipeline_http
from pipeline.http import USER_AGENT, Fetcher, FetchError, RetryLater, RobotsDisallowed, UnsafeURLError

pytestmark = pytest.mark.pipeline


class FakeAdapter(BaseAdapter):
    """Serves canned responses: routes maps a URL to (status, headers, body)."""

    def __init__(self, routes):
        super().__init__()
        self.routes = routes
        self.requests = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        status, headers, body = self.routes.get(request.url, (404, {}, b""))
        response = requests.Response()
        response.status_code = status
        response.headers = CaseInsensitiveDict(headers)
        # What HTTPAdapter hands to requests with stream=True.
        response.raw = urllib3.HTTPResponse(body=io.BytesIO(body), headers=headers, status=status,
                                            preload_content=False)
        response.url = request.url
        response.request = request
        return response

    def close(self):
        pass

    def urls(self):
        return [r.url for r in self.requests]


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_fetcher(routes, min_delay=0.0, clock=None):
    clock = clock or Clock()
    fetcher = Fetcher(min_delay=min_delay, sleep=clock.sleep, clock=clock)
    adapter = FakeAdapter(routes)
    fetcher.session.mount("http://", adapter)
    fetcher.session.mount("https://", adapter)
    return fetcher, adapter, clock


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://site.example/a.jpg",
    "data:image/png;base64,AAAA",
    "http:///no-host",
    "http://127.0.0.1/a.png",
    "http://10.0.0.1/a.png",
    "http://192.168.1.10/a.png",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/a.png",
    "http://[::ffff:127.0.0.1]/a.png",
    "http://0.0.0.0/a.png",
])
def test_rejects_unsafe_urls_without_sending_anything(url):
    fetcher, adapter, _ = make_fetcher({})
    with pytest.raises(UnsafeURLError):
        fetcher.fetch(url)
    assert adapter.requests == []


def test_rejects_hostname_resolving_to_private_address(monkeypatch):
    monkeypatch.setattr(pipeline_http.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 80))])
    fetcher, adapter, _ = make_fetcher({})
    with pytest.raises(UnsafeURLError):
        fetcher.fetch("http://intranet.example/a.png")
    assert adapter.requests == []


def test_redirect_to_private_address_is_rejected(fake_dns):
    fetcher, adapter, _ = make_fetcher({
        "http://site.example/a.png": (302, {"Location": "http://10.0.0.1/secret.png"}, b""),
    })
    with pytest.raises(UnsafeURLError):
        fetcher.fetch("http://site.example/a.png")
    assert "http://10.0.0.1/secret.png" not in adapter.urls()


def test_redirect_to_public_host_is_followed_and_rechecked(fake_dns):
    fetcher, adapter, _ = make_fetcher({
        "http://site.example/a.png": (301, {"Location": "https://cdn.example/b.png"}, b""),
        "https://cdn.example/b.png": (200, {}, b"image-bytes"),
        "https://cdn.example/robots.txt": (200, {}, b"User-agent: *\nDisallow: /private\n"),
    })
    result = fetcher.fetch("http://site.example/a.png")
    assert result.url == "https://cdn.example/b.png"
    assert result.content == b"image-bytes"
    # robots.txt of both hosts was consulted
    assert "http://site.example/robots.txt" in adapter.urls()
    assert "https://cdn.example/robots.txt" in adapter.urls()


@pytest.mark.parametrize("location", ["http://[invalid/x", "http://[SERVER_NAME]/index.html"])
def test_malformed_redirect_raises_fetch_error(fake_dns, location):
    fetcher, _, _ = make_fetcher({"http://site.example/a": (302, {"Location": location}, b"")})
    with pytest.raises(FetchError, match="invalid redirect"):
        fetcher.fetch("http://site.example/a")


def test_redirect_loop_gives_up(fake_dns):
    fetcher, _, _ = make_fetcher({
        "http://site.example/a": (302, {"Location": "/a"}, b""),
    })
    with pytest.raises(FetchError, match="too many redirects"):
        fetcher.fetch("http://site.example/a")


def test_peer_address_is_checked_at_connect_time(monkeypatch):
    """A name that passed the DNS check but connects to loopback (DNS rebinding) is refused."""
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Pretend the up-front DNS check saw a public address.
        monkeypatch.setattr(pipeline_http, "_check_host", lambda host, port: None)
        fetcher = Fetcher(min_delay=0)
        with pytest.raises(UnsafeURLError):
            fetcher.fetch(f"http://127.0.0.1:{server.server_port}/a.png")
    finally:
        server.shutdown()
        server.server_close()
    assert hits == []


def test_sends_identifying_user_agent_and_ignores_environment(fake_dns):
    fetcher, adapter, _ = make_fetcher({"http://site.example/a": (200, {}, b"ok")})
    fetcher.fetch("http://site.example/a")
    assert {r.headers["User-Agent"] for r in adapter.requests} == {USER_AGENT}
    assert "+https://github.com/piculjantechnologies/cortex" in USER_AGENT
    assert fetcher.session.trust_env is False


def test_robots_txt_disallow_is_honoured_and_cached(fake_dns):
    fetcher, adapter, _ = make_fetcher({
        "http://site.example/robots.txt": (200, {}, b"User-agent: *\nDisallow: /private/\n"),
        "http://site.example/public/a.png": (200, {}, b"a"),
        "http://site.example/public/b.png": (200, {}, b"b"),
        "http://site.example/private/c.png": (200, {}, b"c"),
    })
    assert fetcher.fetch("http://site.example/public/a.png").content == b"a"
    assert fetcher.fetch("http://site.example/public/b.png").content == b"b"
    with pytest.raises(RobotsDisallowed):
        fetcher.fetch("http://site.example/private/c.png")
    assert adapter.urls().count("http://site.example/robots.txt") == 1
    assert "http://site.example/private/c.png" not in adapter.urls()


def test_robots_txt_group_for_our_agent(fake_dns):
    fetcher, _, _ = make_fetcher({
        "http://site.example/robots.txt": (200, {}, b"User-agent: CortexBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"),
        "http://site.example/a.png": (200, {}, b"a"),
    })
    with pytest.raises(RobotsDisallowed):
        fetcher.fetch("http://site.example/a.png")


@pytest.mark.parametrize("status,allowed", [(404, True), (410, True), (401, False), (403, False)])
def test_robots_txt_status_policy(fake_dns, status, allowed):
    fetcher, _, _ = make_fetcher({
        "http://site.example/robots.txt": (status, {}, b""),
        "http://site.example/a.png": (200, {}, b"a"),
    })
    if allowed:
        assert fetcher.fetch("http://site.example/a.png").content == b"a"
    else:
        with pytest.raises(RobotsDisallowed):
            fetcher.fetch("http://site.example/a.png")


@pytest.mark.parametrize("status", [429, 500, 503])
def test_unavailable_robots_txt_fails_one_url_and_pauses_the_host(fake_dns, status):
    fetcher, adapter, clock = make_fetcher({
        "http://site.example/robots.txt": (status, {}, b""),
        "http://site.example/a.png": (200, {}, b"a"),
        "http://site.example/b.png": (200, {}, b"b"),
    })
    with pytest.raises(FetchError, match=f"robots.txt unavailable \\(HTTP {status}\\)") as excinfo:
        fetcher.fetch("http://site.example/a.png")
    assert not isinstance(excinfo.value, (RetryLater, RobotsDisallowed))
    # The host's other URLs are not requested and not failed: they wait.
    with pytest.raises(RetryLater):
        fetcher.fetch("http://site.example/b.png")
    assert adapter.urls() == ["http://site.example/robots.txt"]
    # After the pause robots.txt is asked for again (nothing was cached).
    adapter.routes["http://site.example/robots.txt"] = (404, {}, b"")
    clock.now += pipeline_http.DEFAULT_RETRY_AFTER + 1
    assert fetcher.fetch("http://site.example/b.png").content == b"b"
    assert adapter.urls().count("http://site.example/robots.txt") == 2


def test_unreachable_robots_txt_fails_one_url_and_pauses_the_host(fake_dns):
    fetcher, adapter, _ = make_fetcher({"http://site.example/a.png": (200, {}, b"a")})

    def refuse(request, **kwargs):
        adapter.requests.append(request)
        raise requests.ConnectionError("refused")

    adapter.send = refuse
    with pytest.raises(FetchError, match="robots.txt unavailable"):
        fetcher.fetch("http://site.example/a.png")
    with pytest.raises(RetryLater):
        fetcher.fetch("http://site.example/b.png")
    assert adapter.urls() == ["http://site.example/robots.txt"]


@pytest.mark.parametrize("line", ["Crawl-delay: \u00b2", "Request-rate: \u00b2/1"])
def test_unparseable_robots_txt_disallows_the_host(fake_dns, line):
    fetcher, adapter, _ = make_fetcher({
        "http://site.example/robots.txt": (200, {}, f"User-agent: *\n{line}\n".encode()),
        "http://site.example/a.png": (200, {}, b"a"),
    })
    with pytest.raises(RobotsDisallowed):
        fetcher.fetch("http://site.example/a.png")
    assert "http://site.example/a.png" not in adapter.urls()


def test_robots_txt_cache_expires(fake_dns):
    fetcher, adapter, clock = make_fetcher({
        "http://site.example/robots.txt": (404, {}, b""),
        "http://site.example/a.png": (200, {}, b"a"),
    })
    fetcher.fetch("http://site.example/a.png")
    clock.now += pipeline_http.ROBOTS_TTL + 1
    fetcher.fetch("http://site.example/a.png")
    assert adapter.urls().count("http://site.example/robots.txt") == 2


def test_per_host_throttle(fake_dns):
    fetcher, _, clock = make_fetcher({
        "http://site.example/robots.txt": (404, {}, b""),
        "http://site.example/a": (200, {}, b"a"),
        "http://other.example/robots.txt": (404, {}, b""),
        "http://other.example/b": (200, {}, b"b"),
    }, min_delay=1.5)
    fetcher.fetch("http://site.example/a")  # robots.txt, then the page 1.5 s later
    assert clock.sleeps == [1.5]
    fetcher.fetch("http://other.example/b")  # another host: only its own robots.txt gap
    assert clock.sleeps == [1.5, 1.5]
    clock.now += 0.5
    fetcher.fetch("http://site.example/a")  # site's last request was 2 s ago: no wait
    assert clock.sleeps == [1.5, 1.5]
    fetcher.fetch("http://site.example/a")  # immediately again: the full gap
    assert clock.sleeps == [1.5, 1.5, 1.5]


def test_crawl_delay_is_honoured(fake_dns):
    fetcher, _, clock = make_fetcher({
        "http://site.example/robots.txt": (200, {}, b"User-agent: *\nCrawl-delay: 5\n"),
        "http://site.example/a": (200, {}, b"a"),
    }, min_delay=1.0)
    fetcher.fetch("http://site.example/a")
    fetcher.fetch("http://site.example/a")
    assert clock.sleeps[-1] == pytest.approx(5.0)


def test_excessive_crawl_delay_disallows_host(fake_dns):
    fetcher, _, _ = make_fetcher({
        "http://site.example/robots.txt": (200, {}, b"User-agent: *\nCrawl-delay: 3600\n"),
        "http://site.example/a": (200, {}, b"a"),
    })
    with pytest.raises(RobotsDisallowed):
        fetcher.fetch("http://site.example/a")


def test_429_retry_after_pauses_host(fake_dns):
    fetcher, adapter, clock = make_fetcher({
        "http://site.example/robots.txt": (404, {}, b""),
        "http://site.example/a": (429, {"Retry-After": "120"}, b""),
        "http://site.example/b": (200, {}, b"b"),
    })
    with pytest.raises(FetchError, match="HTTP 429"):
        fetcher.fetch("http://site.example/a")
    with pytest.raises(RetryLater, match="paused"):
        fetcher.fetch("http://site.example/b")
    assert "http://site.example/b" not in adapter.urls()
    clock.now += 121
    assert fetcher.fetch("http://site.example/b").content == b"b"


def test_body_size_cap_and_truncation(fake_dns):
    body = b"x" * 1000
    fetcher, _, _ = make_fetcher({
        "http://site.example/robots.txt": (404, {}, b""),
        "http://site.example/big": (200, {}, body),
        "http://site.example/declared": (200, {"Content-Length": "999999"}, b"x"),
    })
    with pytest.raises(FetchError, match="too large"):
        fetcher.fetch("http://site.example/big", max_bytes=999)
    with pytest.raises(FetchError, match="too large"):
        fetcher.fetch("http://site.example/declared", max_bytes=999)
    assert fetcher.fetch("http://site.example/big", max_bytes=16, truncate=True).content == b"x" * 16
    assert fetcher.fetch("http://site.example/big", max_bytes=1000).content == body


def test_gzip_body_is_decoded(fake_dns):
    fetcher, _, _ = make_fetcher({
        "http://site.example/robots.txt": (404, {}, b""),
        "http://site.example/page": (200, {"Content-Encoding": "gzip"}, gzip.compress(b"<html>" * 1000)),
    })
    assert fetcher.fetch("http://site.example/page").content == b"<html>" * 1000
    with pytest.raises(FetchError, match="too large"):
        fetcher.fetch("http://site.example/page", max_bytes=5999)  # the decoded size counts


def test_non_2xx_raises(fake_dns):
    fetcher, _, _ = make_fetcher({"http://site.example/robots.txt": (404, {}, b"")})
    with pytest.raises(FetchError, match="HTTP 404"):
        fetcher.fetch("http://site.example/missing.png")


def test_connection_errors_become_fetch_errors(fake_dns):
    fetcher, adapter, _ = make_fetcher({"http://site.example/robots.txt": (404, {}, b"")})

    def boom(request, **kwargs):
        if request.url.endswith("robots.txt"):
            return FakeAdapter.send(adapter, request, **kwargs)
        raise requests.ConnectionError("refused")

    adapter.send = boom
    with pytest.raises(FetchError, match="ConnectionError"):
        fetcher.fetch("http://site.example/a.png")


@pytest.mark.parametrize("value,expected", [("120", 120.0), (None, 60.0), ("soon", 60.0),
                                            ("-5", 0.0), ("999999", 3600.0)])
def test_retry_after_parsing(value, expected):
    assert pipeline_http._retry_after(value) == expected


# Against a real socket (loopback, allowed for these tests only)

@pytest.fixture
def local_server(monkeypatch):
    """Factory: local_server({path: handler(request_handler, stop_event)}) -> base URL."""
    stop = threading.Event()
    servers = []

    def start(routes):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                route = routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    route(self, stop)
                except OSError:  # the client hung up
                    pass

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    monkeypatch.setattr(pipeline_http, "_check_host", lambda host, port: None)
    monkeypatch.setattr(pipeline_http, "_check_address", lambda address: None)
    yield start
    stop.set()
    for server in servers:
        server.shutdown()
        server.server_close()


def trickle(status, headers, first=b"", byte_every=0.1, limit=5.0):
    """A route that sends `first` at once, then one byte every `byte_every` s (for `limit` s at most)."""
    def route(handler, stop):
        handler.send_response(status)
        for name, value in headers.items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(first)
        handler.wfile.flush()
        ends = time.monotonic() + limit
        while time.monotonic() < ends and not stop.wait(byte_every):
            handler.wfile.write(b"x")
            handler.wfile.flush()
    return route


def test_deadline_stops_a_slow_body(local_server):
    base = local_server({"/slow": trickle(200, {"Content-Length": "100000"})})
    fetcher = Fetcher(min_delay=0, timeout=5, deadline=0.5)
    started = time.monotonic()
    with pytest.raises(FetchError, match="read took too long"):
        fetcher.fetch(base + "/slow")
    assert time.monotonic() - started < 3


def test_sniff_returns_once_the_first_bytes_are_in(local_server):
    head = b"\x89PNG\r\n\x1a\n" + b"\x00" * (pipeline_http.SNIFF_BYTES - 8)
    base = local_server({"/a.png": trickle(200, {"Content-Length": "100000"}, first=head, byte_every=1.0)})
    fetcher = Fetcher(min_delay=0, timeout=5)
    started = time.monotonic()
    result = fetcher.fetch(base + "/a.png", max_bytes=pipeline_http.SNIFF_BYTES, truncate=True)
    assert result.content == head
    assert time.monotonic() - started < 2


def test_redirect_body_is_not_read(local_server):
    def ok(handler, stop):
        handler.send_response(200)
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"ok")

    base = local_server({
        "/r": trickle(302, {"Location": "/ok", "Content-Length": "50000000"}, first=b"x" * 100_000),
        "/ok": ok,
    })
    fetcher = Fetcher(min_delay=0, timeout=5, deadline=1.0)
    started = time.monotonic()
    result = fetcher.fetch(base + "/r", max_bytes=10)
    assert (result.url, result.content) == (base + "/ok", b"ok")
    assert time.monotonic() - started < 2


def test_chunked_body_is_read_and_capped(local_server):
    def chunked(handler, stop):
        handler.protocol_version = "HTTP/1.1"
        handler.send_response(200)
        handler.send_header("Transfer-Encoding", "chunked")
        handler.send_header("Connection", "close")
        handler.end_headers()
        for part in (b"<html>", b"x" * 5000, b"</html>"):
            handler.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
        handler.wfile.write(b"0\r\n\r\n")

    base = local_server({"/page": chunked})
    fetcher = Fetcher(min_delay=0)
    assert fetcher.fetch(base + "/page").content == b"<html>" + b"x" * 5000 + b"</html>"
    with pytest.raises(FetchError, match="too large"):
        fetcher.fetch(base + "/page", max_bytes=5012)
