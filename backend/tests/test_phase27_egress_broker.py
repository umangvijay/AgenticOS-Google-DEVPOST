"""Phase 27 §27.3 — the enforcing outbound broker (audit 27.R1 broker).

The sandbox never gets direct network access; this broker is the only
egress path. Tests: allowed destination passes; metadata, private
ranges, localhost, ungranted hosts, non-HTTPS, and redirect-to-forbidden
are denied; DNS resolution happens in the broker.
"""
import pytest

from backend.runners.egress_broker import (
    EgressBroker, EgressGrant, EgressDeniedError, deny_all)


def test_default_profile_denies_everything():
    broker = deny_all()
    with pytest.raises(EgressDeniedError):
        broker.validate_url("https://example.com/api")


def test_metadata_endpoints_always_denied():
    broker = EgressBroker(EgressGrant(allowed=[("metadata.google.internal", 443)]))
    with pytest.raises(EgressDeniedError):
        broker.validate_url("https://metadata.google.internal/computeMetadata/v1/")
    broker2 = EgressBroker(EgressGrant(allowed=[("169.254.169.254", 443)]))
    with pytest.raises(EgressDeniedError):
        broker2.validate_url("https://169.254.169.254/latest/meta-data/")


def test_private_and_localhost_denied_even_if_granted():
    broker = EgressBroker(EgressGrant(allowed=[
        ("192.168.1.10", 443), ("localhost", 443), ("10.0.0.5", 8443)]))
    for url in ("https://192.168.1.10/x", "https://localhost/x",
                "https://10.0.0.5:8443/x", "http://localhost/x"):
        with pytest.raises(EgressDeniedError):
            broker.validate_url(url)


def test_ungranted_public_host_denied():
    broker = EgressBroker(EgressGrant(allowed=[("api.example.com", 443)]))
    with pytest.raises(EgressDeniedError):
        broker.validate_url("https://other.example.net/v1")


def test_granted_public_host_passes_validation():
    broker = EgressBroker(EgressGrant(allowed=[("example.com", 443)]))
    scheme, host, port = broker.validate_url("https://example.com/v1/data")
    assert (scheme, host, port) == ("https", "example.com", 443)


def test_non_https_denied():
    broker = EgressBroker(EgressGrant(allowed=[("example.com", 80)]))
    with pytest.raises(EgressDeniedError):
        broker.validate_url("http://example.com/v1")


def test_redirect_to_forbidden_host_denied(monkeypatch):
    """A granted host must not be able to bounce the request to a
    forbidden destination — every hop is re-validated."""
    class _Resp:
        status_code = 302
        headers = {"location": "https://evil.example.net/capture"}
        is_redirect = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            return iter([b""])

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *a, **k):
            return _Resp()

    import backend.runners.egress_broker as eb
    monkeypatch.setattr(eb.httpx, "Client", _Client)
    broker = EgressBroker(EgressGrant(allowed=[("example.com", 443)]))
    with pytest.raises(EgressDeniedError):
        broker.request("GET", "https://example.com/redirect-me")


def test_dns_resolution_happens_in_the_broker(monkeypatch):
    """A host that resolves to a private address is denied even when the
    NAME looks public (the sandbox never resolves on its own)."""
    broker = EgressBroker(EgressGrant(allowed=[("internal.example.com", 443)]))
    import backend.runners.egress_broker as eb

    class _FakeAddr:
        class address:  # noqa: N801
            pass

    import ipaddress
    fake = [(None, None, None, "", ("192.168.0.9", 0))]
    monkeypatch.setattr(eb.socket, "getaddrinfo",
                        lambda *a, **k: fake)
    with pytest.raises(EgressDeniedError):
        broker.validate_url("https://internal.example.com/x")


def test_grant_is_port_specific():
    """§27.3: a grant for host:443 does NOT authorize host:8443 — the
    requested port is what the grant must cover (audit finding)."""
    broker = EgressBroker(EgressGrant(allowed=[("example.com", 443)]))
    with pytest.raises(EgressDeniedError):
        broker.validate_url("https://example.com:8443/v1")
    # And the actual granted port validates.
    broker.validate_url("https://example.com/v1")


def test_granted_port_explicit_form():
    broker = EgressBroker(EgressGrant(allowed=[("example.com", 8443)]))
    scheme, host, port = broker.validate_url("https://example.com:8443/v1")
    assert (host, port) == ("example.com", 8443)
    with pytest.raises(EgressDeniedError):
        broker.validate_url("https://example.com/v1")
