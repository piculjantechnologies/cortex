"""Fixtures for the pipeline tests.

Only the standard library and pytest are imported here, so collection works in
the web venv too; each test module skips itself when a pipeline dependency is
missing.
"""
import socket

import pytest

PUBLIC_IP = "93.184.215.14"


@pytest.fixture
def fake_dns(monkeypatch):
    """Resolve every *.example host to a public address; other names resolve normally."""
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if isinstance(host, str) and host.endswith(".example"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port or 80))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def stub_fetcher():
    """Factory: stub_fetcher({url: bytes | Exception}) -> an object with Fetcher.fetch's interface.

    Unknown URLs raise FetchError('HTTP 404'); every call is recorded in .calls.
    """
    from pipeline.http import FetchError, FetchResult

    class StubFetcher:
        def __init__(self, routes):
            self.routes = dict(routes)
            self.calls = []

        def fetch(self, url, max_bytes=None, truncate=False):
            self.calls.append(url)
            body = self.routes.get(url, FetchError("HTTP 404"))
            if isinstance(body, Exception):
                raise body
            if max_bytes is not None and len(body) > max_bytes:
                if not truncate:
                    raise FetchError("too large")
                body = body[:max_bytes]
            return FetchResult(url, body)

    return StubFetcher
