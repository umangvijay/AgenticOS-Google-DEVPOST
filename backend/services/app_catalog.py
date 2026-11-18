"""Phase 27 §27.6 — the DECLARED supported-application catalog.

"Connect an app" is a product flow backed by CATALOG ENTRIES with a
stable app/version ID, publisher, auth method, documented operations,
tested operations and an HONEST qualification status — separate from
custom generation (which remains a prompt/spec entry point into the
same durable build pipeline).

A catalog entry never claims execution proof: per-operation readiness
is derived from the build's validation/activation records (§27.6 work
item 5). `qualification` states what the PUBLISHER declared:
  - "qualified"   — documented + tested operations, verified install path
  - "requires_credentials" — documented but needs a user-bound credential
  - "fixture"     — declared app used by the qualification journey
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

CATALOG_VERSION = 1

_HTTPBIN_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "HTTPBin", "version": "1.0.0"},
    "servers": [{"url": "https://httpbin.org"}],
    "paths": {
        "/uuid": {"get": {"operationId": "get_uuid",
                          "summary": "Return a fresh UUID",
                          "responses": {"200": {"description": "ok"}}}},
        "/get": {"get": {"operationId": "get_echo",
                         "summary": "Echo the request arguments",
                         "responses": {"200": {"description": "ok"}}}},
        "/status/{code}": {"get": {"operationId": "get_status",
                                   "summary": "Return a status code",
                                   "parameters": [{"name": "code", "in": "path",
                                                   "required": True,
                                                   "schema": {"type": "integer"}}],
                                   "responses": {"200": {"description": "ok"}}}},
    },
}

_LEDGER_SPEC_TEMPLATE = {
    "openapi": "3.0.0",
    "info": {"title": "Ledger Demo", "version": "1.0.0"},
    "servers": [{"url": "{base_url}"}],
    "paths": {
        "/entries": {"get": {"operationId": "list_entries",
                             "summary": "List ledger entries",
                             "responses": {"200": {"description": "ok"}}}},
        "/total": {"get": {"operationId": "ledger_total",
                           "summary": "Total of computed entries",
                           "responses": {"200": {"description": "ok"}}}},
    },
}


def _entry(app_id: str, version: str, name: str, publisher: str,
           auth_method: str, documented: List[str], tested: List[str],
           qualification: str, source: Dict[str, Any],
           description: str) -> Dict[str, Any]:
    return {
        "app_id": app_id,
        "version": version,
        "name": name,
        "publisher": publisher,
        "auth_method": auth_method,
        "documented_operations": documented,
        "tested_operations": tested,
        "qualification": qualification,
        "source": source,
        "description": description,
    }


DECLARED_APPS: List[Dict[str, Any]] = [
    _entry(
        "httpbin", "1.0.0", "HTTPBin", "httpbin.org", "none",
        documented=["get_uuid", "get_echo", "get_status"],
        tested=["get_uuid", "get_echo"],
        qualification="qualified",
        source={"kind": "spec", "spec_format": "json",
                "spec": _HTTPBIN_SPEC},
        description="Public HTTP request/response inspection API. "
                    "Read-only GET operations; no authentication."),
    _entry(
        "ledger-demo", "1.0.0", "Ledger Demo", "agentos-qualification",
        "none",
        documented=["list_entries", "ledger_total"],
        tested=["list_entries"],
        qualification="fixture",
        source={"kind": "spec_template", "spec_format": "json",
                "base_url_param": "base_url",
                "spec": _LEDGER_SPEC_TEMPLATE},
        description="Controlled fixture app for the install journey; "
                    "its base URL is supplied at install time."),
]


def app_by_id(app_id: str) -> Optional[Dict[str, Any]]:
    for app in DECLARED_APPS:
        if app["app_id"] == app_id:
            return dict(app)
    return None


def install_source(app: Dict[str, Any],
                   base_url: Optional[str] = None) -> str:
    """Resolve the catalog entry's source into the durable build's
    `source` string (the OpenAPI spec). Template specs require the
    caller to bind the base URL at install time."""
    source = app["source"]
    if source["kind"] == "spec":
        import json
        return json.dumps(source["spec"])
    if source["kind"] == "spec_template":
        if not base_url:
            raise ValueError(
                f"app {app['app_id']} requires a base_url at install time")
        import json
        spec = json.loads(json.dumps(source["spec"]))
        spec["servers"] = [{"url": base_url}]
        return json.dumps(spec)
    raise ValueError(f"unsupported catalog source kind: {source['kind']}")


def catalog_public_view() -> List[Dict[str, Any]]:
    """The published catalog view — never includes raw spec bytes or
    anything that would pass off a declaration as execution proof."""
    out = []
    for app in DECLARED_APPS:
        out.append({
            "app_id": app["app_id"],
            "version": app["version"],
            "name": app["name"],
            "publisher": app["publisher"],
            "auth_method": app["auth_method"],
            "documented_operations": app["documented_operations"],
            "tested_operations": app["tested_operations"],
            "qualification": app["qualification"],
            "description": app["description"],
            "catalog_version": CATALOG_VERSION,
        })
    return out
