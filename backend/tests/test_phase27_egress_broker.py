"""Phase 27 §27.3 — the enforcing outbound broker (audit 27.R1 broker).

The sandbox never gets direct network access; this broker is the only
egress path. Tests: allowed destination passes; metadata, private
ranges, localhost, ungranted hosts, non-HTTPS, and redirect-to-forbidden
are denied; DNS resolution happens in the broker.
"""
import socket

import pytest

from backend.runners.egress_broker import (
    EgressBroker, EgressGrant, EgressDeniedError, deny_all)

# example.com's documented A address: the ALLOW-path tests exercise the
# broker's grant/port/redirect LOGIC, not the network. A deterministic
# resolver keeps them hermetic (CI runs with --network=none, where real
# DNS cannot resolve) while the deny-on-resolution-failure path keeps
# explicit coverage below.
_PUBLIC_EXAMPLE = [("93.184.216.34", 0)]


def _patch_public_resolver(monkeypatch):
    """Replace the OS resolver with a deterministic public answer."""
    import backend.runners.egress_broker as eb
    monkeypatch.setattr(
        eb.socket, "getaddrinfo",
        lambda host, port, *a, **k: [(None, None, None, "", _PUBLIC_EXAMPLE[0])])


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


def test_granted_public_host_passes_validation(monkeypatch):
    _patch_public_resolver(monkeypatch)
    broker = EgressBroker(EgressGrant(allowed=[("example.com", 443)]))
    scheme, host, port = broker.validate_url("https://example.com/v1/data")
    assert (scheme, host, port) == ("https", "example.com", 443)


def test_resolution_failure_denies_even_granted_hosts(monkeypatch):
    """§27.3 fail-closed: when the broker cannot resolve a destination
    (offline CI, DNS outage), a GRANTED host is still denied — the
    sandbox never sends traffic to an unverified address."""
    import backend.runners.egress_broker as eb

    def _no_dns(*a, **k):
        # getaddrinfo's actual offline failure mode is socket.gaierror
        # (Errno -3, temporary failure in name resolution).
        raise socket.gaierror(-3, "Temporary failure in name resolution")
    monkeypatch.setattr(eb.socket, "getaddrinfo", _no_dns)
    broker = EgressBroker(EgressGrant(allowed=[("example.com", 443)]))
    with pytest.raises(EgressDeniedError):
        broker.validate_url("https://example.com/v1")


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


def test_grant_is_port_specific(monkeypatch):
    """§27.3: a grant for host:443 does NOT authorize host:8443 — the
    requested port is what the grant must cover (audit finding)."""
    _patch_public_resolver(monkeypatch)
    broker = EgressBroker(EgressGrant(allowed=[("example.com", 443)]))
    with pytest.raises(EgressDeniedError):
        broker.validate_url("https://example.com:8443/v1")
    # And the actual granted port validates.
    broker.validate_url("https://example.com/v1")


def test_granted_port_explicit_form(monkeypatch):
    _patch_public_resolver(monkeypatch)
    broker = EgressBroker(EgressGrant(allowed=[("example.com", 8443)]))
    scheme, host, port = broker.validate_url("https://example.com:8443/v1")
    assert (host, port) == ("example.com", 8443)
    with pytest.raises(EgressDeniedError):
        broker.validate_url("https://example.com/v1")
