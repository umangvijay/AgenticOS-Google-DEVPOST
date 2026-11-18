"""Phase 27 §27.3 — the enforcing outbound-access broker.

Generated code runs with NO network access by default. When an artifact
declares outbound grants, the broker is the ONLY path through which
egress happens: the container has no direct route; the broker proxies
allow-listed destinations, resolves DNS itself, and enforces the policy
at the connection boundary.

Policy enforced here (MCP security guidance + §27.3):
- explicit allow-list of hosts/ports per execution;
- metadata and private/link-local addresses are always denied;
- redirects are re-validated against the same allow-list (a permitted
  host may not redirect to a forbidden one);
- raw sockets from inside the sandbox are impossible by construction —
  the broker is a process boundary, not a convention.

The broker NEVER opens arbitrary tunnels: only outbound HTTPS with the
declared method/path/body forwarding.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urljoin

import httpx

METADATA_HOSTS = {"metadata.google.internal", "169.254.169.254",
                  "metadata", "100.100.100.200"}
_DNS_SLOTS = threading.BoundedSemaphore(16)


class EgressDeniedError(Exception):
    """The destination or a redirect target is not in the grant."""


@dataclass
class EgressGrant:
    """Per-execution outbound grant: exact host:port pairs."""
    allowed: list[tuple[str, int]] = field(default_factory=list)

    def allows(self, host: str, port: int) -> bool:
        return (str(host).lower(), int(port)) in {
            (h.lower(), p) for h, p in self.allowed}


def _resolve(host, port, check=None):
    if check is None:
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    check()
    # OS resolver calls cannot be interrupted. Bound abandoned calls and keep
    # the supervisor cancellable while the resolver finishes independently.
    if not _DNS_SLOTS.acquire(blocking=False):
        raise EgressDeniedError("Broker resolver capacity exhausted")
    done, result = threading.Event(), {}
    def resolve():
        try:
            result["answers"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception as exc:
            result["error"] = exc
        finally:
            _DNS_SLOTS.release()
            done.set()
    threading.Thread(target=resolve, name="broker-dns", daemon=True).start()
    dns_deadline = time.monotonic() + 2
    while not done.wait(.05):
        check()
        if time.monotonic() >= dns_deadline:
            raise EgressDeniedError("Broker DNS deadline exceeded")
    check()
    if "error" in result:
        raise EgressDeniedError("Destination resolution failed") from result["error"]
    return result["answers"]


def _public_and_allowed(host: str, port: int, grant: EgressGrant, check=None) -> bool:
    """A destination must resolve to a public address AND be granted for
    the REQUESTED host AND port (§27.3: the grant is per host:port — the
    previous check compared against a scheme-derived 443 regardless of
    the URL's actual port)."""
    try:
        answers = _resolve(host, port, check)
    except (socket.gaierror, ValueError):
        return False
    for answer in answers:
        ip = ipaddress.ip_address(answer[4][0].split("%")[0])
        if not ip.is_global:
            return False
    return grant.allows(host, port)


class EgressBroker:
    """Enforcing outbound proxy for sandboxed executions."""

    def __init__(self, grant: EgressGrant, *, deadline=None, authority_check=None):
        self.grant = grant
        self.deadline = deadline
        self.authority_check = authority_check

    def validate_url(self, url: str, *, check=None) -> tuple[str, str, int]:
        """Validate BEFORE any request; raise EgressDeniedError on the
        metadata service, private ranges, or ungranted destinations.
        The REQUESTED port is what the grant must cover."""
        parsed = urlsplit(url or "")
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.username is not None or parsed.password is not None:
            raise EgressDeniedError("URL credentials are not permitted")
        if parsed.scheme != "https":
            raise EgressDeniedError("only HTTPS egress is brokered")
        if host in METADATA_HOSTS:
            raise EgressDeniedError("metadata endpoints are denied")
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            addr = None
        if addr is not None and not addr.is_global:
            raise EgressDeniedError("private network addresses are denied")
        if ".localhost" in host or host == "localhost":
            raise EgressDeniedError("localhost is denied")
        if not self.grant.allows(host, port) or not _public_and_allowed(host, port, self.grant, check):
            raise EgressDeniedError(
                f"destination {host}:{port} is not in the execution grant")
        return parsed.scheme, host, port

    def request(self, method: str, url: str, *, json_body=None,
                headers: dict | None = None,
                max_redirects: int = 3) -> httpx.Response:
        """The ONLY egress path: validate, resolve to a VETTED address,
        connect to that address (TLS still verifies the original
        hostname via SNI/HTTP host header), and forward. Redirects are
        re-validated hop by hop; a redirect that changes origin drops
        destination-bound credentials."""
        current = url
        deadline = min(self.deadline or float("inf"), time.monotonic() + 30)
        def check():
            if time.monotonic() >= deadline:
                raise EgressDeniedError("Broker total deadline exceeded")
            if self.authority_check is not None and not self.authority_check():
                raise EgressDeniedError("Broker execution authority ended")
            return max(.001, min(2, deadline - time.monotonic()))
        check()
        forwarded = dict(headers or {})
        if any(key.lower() in ("host", "proxy-authorization", "content-length", "transfer-encoding") for key in forwarded):
            raise EgressDeniedError("Connection headers are controlled by the broker")
        method = method.upper()
        if method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            raise EgressDeniedError("Unsupported broker method")
        if not isinstance(max_redirects, int) or not 0 <= max_redirects <= 5:
            raise EgressDeniedError("Invalid redirect budget")
        for hop in range(max_redirects + 1):
            _, host, port = self.validate_url(current, check=check)
            vetted = self._vetted_address(host, port, check=check)
            original = httpx.URL(current)
            # Connect to the vetted numeric address. httpcore uses sni_hostname
            # for TLS hostname verification; Host retains the granted origin.
            # No subsequent hostname lookup can redirect the connection.
            pinned = original.copy_with(host=vetted)
            authority = ("[" + host + "]") if ":" in host else host
            if port != 443:
                authority += ":" + str(port)
            request_headers = {k: v for k, v in forwarded.items() if k.lower() != "accept-encoding"}
            request_headers.update({"Host": authority, "Accept-Encoding": "identity"})
            with httpx.Client(follow_redirects=False, timeout=check(), trust_env=False) as client:
                with client.stream(method, pinned, json=json_body, headers=request_headers,
                                   extensions={"sni_hostname": host}) as response:
                    # Do not let automatic decompression allocate an unbounded
                    # expansion before the decoded-byte limit can inspect it.
                    if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                        raise EgressDeniedError("Compressed broker responses are not supported")
                    chunks, total = [], 0
                    for chunk in response.iter_bytes():
                        check()
                        total += len(chunk)
                        if total > 1_000_000:
                            raise EgressDeniedError("Broker response limit exceeded")
                        chunks.append(chunk)
                    # Report the logical URL, never the pinned transport URL.
                    # iter_bytes already decoded content encodings. Retaining
                    # those headers would decode the reconstructed body twice.
                    decoded_headers = {k: v for k, v in response.headers.items()
                                       if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")}
                    resp = httpx.Response(response.status_code, headers=decoded_headers,
                        content=b"".join(chunks), request=httpx.Request(method, current))
            check()
            if not resp.is_redirect:
                return resp
            if hop == max_redirects or not resp.headers.get("location"):
                raise EgressDeniedError("redirect budget exhausted inside the broker")
            target = urljoin(current, resp.headers["location"])
            # Validate even a same-host downgrade or userinfo-bearing redirect.
            _, next_host, next_port = self.validate_url(target, check=check)
            if (host, port) != (next_host, next_port):
                if method not in ("GET", "HEAD"):
                    raise EgressDeniedError("Cross-origin redirects cannot forward request bodies")
                forwarded = {}  # includes custom API-key headers, not just Authorization
                json_body = None
            if resp.status_code == 303 or resp.status_code in (301, 302) and method == "POST":
                method, json_body = "GET", None
                forwarded = {k: v for k, v in forwarded.items() if k.lower() != "content-type"}
            current = target
        raise EgressDeniedError("redirect budget exhausted inside the broker")

    def _vetted_address(self, host: str, port: int, *, check=None) -> str:
        """Resolve HERE and return the vetted IP:port the connection
        must use."""
        try:
            answers = _resolve(host, port, check)
            addresses = [str(ipaddress.ip_address(answer[4][0].split("%")[0])) for answer in answers]
        except (socket.gaierror, ValueError) as exc:
            raise EgressDeniedError("Destination resolution failed") from exc
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise EgressDeniedError(f"{host} did not resolve exclusively to public addresses")
        return addresses[0]



def deny_all() -> EgressBroker:
    """The default profile: every destination denied (network-disabled
    execution keeps working without this module)."""
    return EgressBroker(EgressGrant(allowed=[]))
