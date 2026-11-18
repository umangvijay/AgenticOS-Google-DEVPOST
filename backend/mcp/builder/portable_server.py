"""Export registered HTTP operations as a standalone, inspectable MCP server."""
import io
import json
import zipfile

SERVER_SOURCE = r'''import asyncio
import base64
import ipaddress
import json
import os
import socket
from pathlib import Path
from urllib.parse import quote, urlsplit
import httpx
import jsonschema
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ListToolsResult, CallToolResult

CONFIG = json.loads(Path(__file__).with_name("tools.json").read_text())
TOOLS = {tool["tool_name"]: tool for tool in CONFIG["tools"]}

def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTPS destinations are supported")
    for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(item[4][0]).is_global:
            raise ValueError("Private destinations are prohibited")

async def list_tools(ctx, params):
    return ListToolsResult(tools=[Tool(name=t["tool_name"], description=t.get("description", ""), input_schema=t["input_schema"]) for t in TOOLS.values()])

async def call_tool(ctx, params):
    tool = TOOLS.get(params.name)
    if not tool:
        raise ValueError("Unknown tool")
    args = params.arguments or {}
    jsonschema.validate(args, tool["input_schema"])
    op = tool["operation"]
    method = op["http_method"].upper()
    if method not in ("GET", "HEAD", "OPTIONS") and os.environ.get("MCP_ALLOW_WRITES") != "1":
        raise ValueError("Writes are disabled; explicitly configure MCP_ALLOW_WRITES=1 to enable them")
    base = op["servers"][0]["url"]
    validate_url(base)
    path, query, headers = op["path"], {}, {}
    names = set()
    for parameter in op.get("parameters", []):
        name = parameter["name"]
        names.add(name)
        if name not in args:
            continue
        value = args[name]
        if parameter.get("in") == "path":
            path = path.replace("{" + name + "}", quote(str(value), safe=""))
        elif parameter.get("in") == "header":
            if name.lower() in ("host", "authorization", "cookie"):
                raise ValueError("Sensitive headers must use configured authentication")
            headers[name] = str(value)
        else:
            query[name] = value
    url = base.rstrip("/") + "/" + path.lstrip("/")
    validate_url(url)
    auth = CONFIG.get("auth", {})
    secret = os.environ.get("MCP_API_KEY", "")
    kind = auth.get("type", "NONE").upper()
    if kind != "NONE" and secret:
        if kind == "BASIC":
            headers["Authorization"] = "Basic " + base64.b64encode(secret.encode()).decode()
        elif auth.get("in") == "query":
            query[auth.get("name", "api_key")] = secret
        else:
            name = auth.get("name") or "Authorization"
            headers[name] = ("Bearer " if name.lower() == "authorization" else "") + secret
    body = {k: v for k, v in args.items() if k not in names}
    if "request_body" in body and len(body) == 1:
        body = body["request_body"]
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        async with client.stream(method, url, params=query, headers=headers, json=body or None) as response:
            if response.is_redirect:
                raise ValueError("Redirects require reviewing the registered server URL")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 5 * 1024 * 1024:
                    raise ValueError("Response exceeds 5 MB")
                chunks.append(chunk)
            text = b"".join(chunks).decode("utf-8", errors="replace")
            try:
                data = json.loads(text)
            except ValueError:
                data = text
            return CallToolResult(content=[TextContent(type="text", text=json.dumps({"status":response.status_code,"body":data}))], is_error=response.is_error)

server = Server(CONFIG["name"], on_list_tools=list_tools, on_call_tool=call_tool)
async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
if __name__ == "__main__":
    asyncio.run(main())
'''


def export_server(manifest: dict, tools: list[dict]) -> bytes:
    if not tools or any((t.get('operation') or {}).get('kind') == 'browser' for t in tools):
        raise ValueError('Standalone export requires HTTP API tools; browser tools run inside AgentOS')
    auth = manifest.get('auth') or {}
    if isinstance(auth, str):
        auth = json.loads(auth)
    config = {'name': manifest.get('name') or 'Generated MCP',
              'auth': {k: auth[k] for k in ('type', 'in', 'name') if k in auth},
              'tools': [{k: t[k] for k in ('tool_name', 'description', 'input_schema', 'operation') if k in t} for t in tools]}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('server.py', SERVER_SOURCE)
        archive.writestr('tools.json', json.dumps(config, indent=2))
        archive.writestr('requirements.txt', 'mcp==2.0.0\nhttpx==0.28.1\njsonschema==4.26.0\n')
        archive.writestr('README.md', '# Generated MCP server\n\nRequires Python 3.11+. Install requirements, then run `python server.py` through your MCP client using stdio transport.\n\nSet `MCP_API_KEY` in the server environment for authenticated APIs. No AgentOS credentials are exported. Writes are disabled unless `MCP_ALLOW_WRITES=1` is configured. Review tools.json before enabling writes. Redirects are rejected. The client should require human approval for destructive operations.\n')
    return stream.getvalue()
