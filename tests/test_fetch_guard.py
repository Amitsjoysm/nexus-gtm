"""A service that fetches any URL you name is an open proxy and a read oracle.

`nexus/sources/safety.py` makes this argument about DSNs; it is the same argument, so the same host
rules apply — reused, not copied, so the two cannot drift. Resolve-then-check, because a public name
can point at loopback.
"""
from __future__ import annotations

import socket

import pytest

from nexus.fetching.guard import UrlRejected, check_url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/",
    "http://localhost/admin",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata, by address
    "http://metadata.google.internal/",           # cloud metadata, by name
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://[::1]/",
    "file:///etc/passwd",
    "gopher://x.test/",
])
def test_private_and_non_http_targets_are_refused(url):
    with pytest.raises(UrlRejected):
        check_url(url)


def test_a_public_page_is_allowed(monkeypatch):
    # Offline: the suite must never touch DNS, so the public address is supplied here.
    real = socket.getaddrinfo

    def fake(host, *args, **kwargs):
        if host == "acme.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert check_url("https://acme.test/pricing") == "https://acme.test/pricing"


def test_a_public_name_that_resolves_to_loopback_is_refused(monkeypatch):
    # The DNS-rebinding shape: the name looks public, the address is ours.
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
    )
    with pytest.raises(UrlRejected):
        check_url("https://innocent.example/")


def test_a_url_with_credentials_is_refused():
    # user:pass@host is how an SSRF payload smuggles a different authority past a naive parser.
    with pytest.raises(UrlRejected):
        check_url("https://user:pass@acme.test/")
