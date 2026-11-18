"""
AgentOS — OpenAPI server URL resolution tests

Regression for the audit's L4 finding: petstore3.swagger.io (servers:
[{"url": "/api/v3"}]) was falsely rejected with "OpenAPI spec has no
HTTPS servers" because the parser dropped relative URLs it could not
SSRF-validate. Relative URLs are valid OpenAPI 3 and must survive
parsing, resolve against the spec's own URL, and stay SSRF-guarded.
"""

import pytest
import socket
import ipaddress

from backend.mcp.builder.openapi_parser import OpenAPIParser, SSRFViolationError
from backend.agents.mcp_factory.mcp_factory_agent import MCPFactoryAgent


PETSTORE_LIKE = """
openapi: 3.0.2
info:
  title: Petstore
  version: 1.0.0
servers:
  - url: /api/v3
paths:
  /pets:
    get:
      operationId: listPets
      responses:
        '200':
          description: ok
"""


@pytest.fixture(autouse=True)
def deterministic_dns(monkeypatch):
    """Exercise SSRF validation without depending on public DNS availability."""
    def resolve(host, port, **kwargs):
        try:
            address = str(ipaddress.ip_address(host))
        except ValueError:
            if host != "petstore3.swagger.io":
                raise socket.gaierror("Unmapped test host")
            address = "8.8.8.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def test_relative_servers_survive_parsing():
    model = OpenAPIParser().parse_text(PETSTORE_LIKE)
    assert model.servers, "relative server must be kept"
    assert model.servers[0].url == "/api/v3"


def test_relative_servers_flow_into_generated_tools():
    from backend.mcp.builder.schema_generator import SchemaGenerator

    model = OpenAPIParser().parse_text(PETSTORE_LIKE)
    MCPFactoryAgent._resolve_relative_servers(model, "https://petstore3.swagger.io/openapi.json")
    tools = SchemaGenerator().generate(model, "mcp_test", "1.0.0")
    assert tools, "tool must be generated"
    assert tools[0].operation["servers"][0]["url"] == "https://petstore3.swagger.io/api/v3"


def test_relative_servers_resolve_against_spec_url():
    model = OpenAPIParser().parse_text(PETSTORE_LIKE)
    MCPFactoryAgent._resolve_relative_servers(model, "https://petstore3.swagger.io/openapi.json")
    assert model.servers[0].url == "https://petstore3.swagger.io/api/v3"


def test_scheme_relative_server_cannot_escape_origin():
    spec = PETSTORE_LIKE.replace("url: /api/v3", "url: //169.254.169.254/latest/meta-data")
    model = OpenAPIParser().parse_text(spec)
    with pytest.raises(SSRFViolationError):
        MCPFactoryAgent._resolve_relative_servers(model, "https://petstore3.swagger.io/openapi.json")


def test_factory_rejects_relative_spec_without_source_url_with_clear_error():
    from backend.mcp.builder.openapi_parser import OpenAPIParserError

    model = OpenAPIParser().parse_text(PETSTORE_LIKE)
    with pytest.raises(ValueError, match="no source URL"):
        MCPFactoryAgent._resolve_relative_servers(model, None)


def test_absolute_https_servers_still_work():
    # DNS is deterministic, while the real address validation still runs.
    spec = PETSTORE_LIKE.replace("url: /api/v3", "url: https://petstore3.swagger.io/api/v3")
    model = OpenAPIParser().parse_text(spec)
    assert model.servers[0].url == "https://petstore3.swagger.io/api/v3"
    # Private absolute hosts are still dropped by the parser
    bad = PETSTORE_LIKE.replace("url: /api/v3", "url: http://169.254.169.254/latest")
    assert not OpenAPIParser().parse_text(bad).servers
