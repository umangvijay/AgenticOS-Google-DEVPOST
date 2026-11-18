"""Generate an isolated stdio MCP server from named Python tool functions.

The Python module is untrusted input (including when another agent wrote it).
It is never imported by the control plane. Workloads have no direct network,
credentials or third-party dependencies. Granted anonymous reads use a host
broker. Unsupported schemas fail
before admission rather than silently losing validation constraints.
"""
import ast
import hashlib
import json
import re
from urllib.parse import urlsplit


def artifact_digest(artifact):
    material = {key: artifact.get(key) for key in
                ("format", "files", "tools", "entrypoint", "runtime_image_digest", "read_grants")}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _check_schema(schema, depth=0):
    supported = {"type", "properties", "required", "additionalProperties", "items",
                 "enum", "minimum", "maximum", "minItems", "maxItems", "minLength",
                 "maxLength", "description", "title"}
    if depth > 12 or not isinstance(schema, dict) or set(schema) - supported:
        raise ValueError("Unsupported compute-tool schema; no constraints may be ignored")
    if schema.get("type") not in ("object", "array", "string", "number", "integer", "boolean", "null"):
        raise ValueError("Compute-tool schemas require an explicit supported type")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise ValueError("additionalProperties must be boolean")
    for child in schema.get("properties", {}).values():
        _check_schema(child, depth + 1)
    if "items" in schema:
        _check_schema(schema["items"], depth + 1)
    if schema["type"] == "array" and "items" not in schema:
        raise ValueError("Array items need an explicit schema")


def generate(request, image):
    module = request.get("module", "")
    if not isinstance(module, str) or len(module.encode()) > 100_000:
        raise ValueError("Tool module missing or too large")
    parsed = ast.parse(module)
    functions = {node.name for node in parsed.body if isinstance(node, ast.FunctionDef)}
    tools = request.get("tools")
    if not isinstance(tools, list) or not 1 <= len(tools) <= 20:
        raise ValueError("Declare between one and twenty compute tools")
    normalized, seen = [], set()
    for tool in tools:
        name = tool.get("name", "")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) or name in seen or name not in functions:
            raise ValueError("Each unique tool name must identify a module function")
        schema = tool.get("inputSchema")
        _check_schema(schema)
        if schema["type"] != "object":
            raise ValueError("MCP tool arguments must be an object")
        # Validate schemas with the full host-side implementation as well.
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(schema)
        normalized.append({"name": name, "description": str(tool.get("description", "")), "inputSchema": schema})
        seen.add(name)
    files = {"server.py": SERVER, "implementation.py": module,
             "tools.json": json.dumps(normalized, sort_keys=True, allow_nan=False)}
    grants = request.get("read_grants", [])
    if not isinstance(grants, list) or len(grants) > 10:
        raise ValueError("At most ten exact HTTPS origins can be granted")
    normalized_grants = []
    for grant in grants:
        parsed = urlsplit(grant)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Read grants must be exact HTTPS origins without credentials or paths")
        host = parsed.hostname.rstrip(".").lower()
        normalized_grants.append({"host": host, "port": parsed.port or 443})
    if normalized_grants:
        files["agentos_http.py"] = BROKER_CLIENT
    artifact = {"format": "mcp_stdio_v1", "files": files, "tools": normalized,
                "entrypoint": "server.py", "runtime_image_digest": image,
                "protocol_validation_status": "not_run", "isolation_validation_status": "not_run",
                "read_grants": normalized_grants}
    artifact["content_digest"] = artifact_digest(artifact)
    return artifact


BROKER_CLIENT = r'''import json
import sys
import uuid

def get(url):
    identity = "broker-" + uuid.uuid4().hex
    print(json.dumps({"jsonrpc": "2.0", "id": identity, "method": "agentos/http_read",
                      "params": {"url": url}}), flush=True)
    reply = json.loads(sys.stdin.readline())
    if reply.get("id") != identity or reply.get("jsonrpc") != "2.0":
        raise ValueError("Broker response identity mismatch")
    if "error" in reply:
        raise ValueError(reply["error"].get("message", "Broker refused request"))
    return reply["result"]
'''


SERVER = r'''import json
import math
import sys
import implementation

TOOLS = {item["name"]: item for item in json.load(open("tools.json"))}

def validate(value, schema):
    kind = schema["type"]
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "boolean": isinstance(value, bool),
        "null": value is None,
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool)}
    if not matches[kind] or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError("Invalid argument type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Argument outside enum")
    if kind == "object":
        if any(key not in value for key in schema.get("required", [])):
            raise ValueError("Required argument missing")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError("Unknown argument")
        for key, child in properties.items():
            if key in value:
                validate(value[key], child)
    if kind == "array":
        for item in value:
            validate(item, schema["items"])
    for lower, upper, actual in (("minimum", "maximum", value) if kind in ("integer", "number") else
                                ("minItems", "maxItems", len(value)) if kind == "array" else
                                ("minLength", "maxLength", len(value)) if kind == "string" else
                                ("_none", "_none", 0),):
        if lower in schema and actual < schema[lower] or upper in schema and actual > schema[upper]:
            raise ValueError("Argument outside bounds")

initialized = False
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        if message.get("method") == "notifications/initialized":
            initialized = True
        continue
    response = {"jsonrpc": "2.0", "id": message["id"]}
    method, params = message.get("method"), message.get("params") or {}
    try:
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "AgentOS generated compute server", "version": "1.0"}}
        elif not initialized:
            raise ValueError("Initialize the session first")
        elif method == "tools/list":
            result = {"tools": list(TOOLS.values())}
        elif method == "tools/call":
            tool = TOOLS.get(params.get("name"))
            if tool is None:
                raise ValueError("Unknown tool")
            args = params.get("arguments", {})
            validate(args, tool["inputSchema"])
            try:
                value = getattr(implementation, tool["name"])(**args)
                encoded = json.dumps(value, allow_nan=False)
                result = {"content": [{"type": "text", "text": encoded}], "isError": False}
                if isinstance(value, dict):
                    result["structuredContent"] = value
            except Exception as exc:
                result = {"content": [{"type": "text", "text": str(exc)[:500]}], "isError": True}
        elif method == "ping":
            result = {}
        else:
            response["error"] = {"code": -32601, "message": "Unknown method"}
            result = None
        if "error" not in response:
            response["result"] = result
    except Exception as exc:
        response["error"] = {"code": -32602, "message": str(exc)[:500]}
    print(json.dumps(response, allow_nan=False), flush=True)
'''
