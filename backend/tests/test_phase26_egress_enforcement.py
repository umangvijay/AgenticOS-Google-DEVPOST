"""The actual request must use the approved numeric destination, with TLS SNI."""
from contextlib import contextmanager
import threading
import time

import httpx
import pytest

from backend.runners import egress_broker as eb


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr(eb.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", a[1]))])
    requests, responses = [], []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        @contextmanager
        def stream(self, method, url, **kwargs):
            requests.append((method, url, kwargs))
            yield responses.pop(0)
    monkeypatch.setattr(eb.httpx, "Client", Client)
    return requests, responses


def test_connection_uses_vetted_ip_and_original_tls_identity(transport):
    requests, responses = transport
    responses.append(httpx.Response(200, content=b"actual body"))
    result = eb.EgressBroker(eb.EgressGrant([("api.example.com", 8443)])).request("GET", "https://api.example.com:8443/items")
    method, url, kwargs = requests[0]
    assert url.host == "93.184.216.34" and url.port == 8443
    assert kwargs["headers"]["Host"] == "api.example.com:8443"
    assert kwargs["extensions"]["sni_hostname"] == "api.example.com"
    assert result.request.url.host == "api.example.com" and result.content == b"actual body"


def test_cross_origin_redirect_drops_all_credential_headers(transport):
    requests, responses = transport
    responses.extend([httpx.Response(302, headers={"location": "https://other.example.com/new"}), httpx.Response(200)])
    eb.EgressBroker(eb.EgressGrant([("api.example.com", 443), ("other.example.com", 443)])).request(
        "GET", "https://api.example.com/start", headers={"Authorization": "fixture", "X-Custom-Key": "fixture"})
    assert requests[1][2]["headers"] == {"Host": "other.example.com", "Accept-Encoding": "identity"}


def test_same_host_redirect_cannot_downgrade_or_include_credentials(transport):
    requests, responses = transport
    for target in ("http://api.example.com/new", "https://user:pass@api.example.com/new"):
        responses.append(httpx.Response(302, headers={"location": target}))
        with pytest.raises(eb.EgressDeniedError):
            eb.EgressBroker(eb.EgressGrant([("api.example.com", 443)])).request("GET", "https://api.example.com/start")
    assert len(requests) == 2


def test_rebinding_between_validation_and_connect_is_denied(monkeypatch, transport):
    answers = iter(["93.184.216.34", "127.0.0.1"])
    monkeypatch.setattr(eb.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (next(answers), a[1]))])
    with pytest.raises(eb.EgressDeniedError):
        eb.EgressBroker(eb.EgressGrant([("api.example.com", 443)])).request("GET", "https://api.example.com/start")
    assert transport[0] == []


def test_response_is_bounded_while_reading(transport):
    transport[1].append(httpx.Response(200, content=b"x" * 1_000_001))
    with pytest.raises(eb.EgressDeniedError, match="response limit"):
        eb.EgressBroker(eb.EgressGrant([("api.example.com", 443)])).request("GET", "https://api.example.com/start")


def test_compressed_body_is_refused_before_decoding(transport):
    class MustNotRead(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("Untrusted compressed bytes must not be decoded")
            yield b""
    transport[1].append(httpx.Response(200, headers={"content-encoding": "gzip"}, stream=MustNotRead()))
    with pytest.raises(eb.EgressDeniedError, match="Compressed"):
        eb.EgressBroker(eb.EgressGrant([("api.example.com", 443)])).request("GET", "https://api.example.com/start")
    assert transport[0][0][2]["headers"]["Accept-Encoding"] == "identity"


def test_slow_stream_cannot_extend_total_deadline(monkeypatch, transport):
    clock = {"now": 100.0}
    monkeypatch.setattr(eb.time, "monotonic", lambda: clock["now"])
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(10):
                clock["now"] += 1
                yield b"x"
    transport[1].append(httpx.Response(200, stream=Stream()))
    with pytest.raises(eb.EgressDeniedError, match="total deadline"):
        eb.EgressBroker(eb.EgressGrant([("api.example.com", 443)]), deadline=103).request(
            "GET", "https://api.example.com/start")
    assert clock["now"] == 103


def test_cancelled_execution_cannot_follow_redirect(transport):
    authorized = {"value": True}
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            authorized["value"] = False
            yield b"redirect"
    transport[1].append(httpx.Response(302, headers={"location": "/another"}, stream=Stream()))
    with pytest.raises(eb.EgressDeniedError, match="authority ended"):
        eb.EgressBroker(eb.EgressGrant([("api.example.com", 443)]),
            authority_check=lambda: authorized["value"]).request("GET", "https://api.example.com/start")
    assert len(transport[0]) == 1


def test_pending_dns_does_not_block_execution_cancellation(monkeypatch, transport):
    resolving, release = threading.Event(), threading.Event()
    def resolver(*args, **kwargs):
        resolving.set()
        release.wait(3)
        return [(2, 1, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(eb.socket, "getaddrinfo", resolver)
    started = time.monotonic()
    try:
        with pytest.raises(eb.EgressDeniedError, match="authority ended"):
            eb.EgressBroker(eb.EgressGrant([("api.example.com", 443)]),
                authority_check=lambda: not resolving.is_set()).request("GET", "https://api.example.com/start")
        assert time.monotonic() - started < 1
        assert transport[0] == []
    finally:
        release.set()
