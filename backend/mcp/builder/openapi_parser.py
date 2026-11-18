import json
import yaml
import urllib.parse
import ipaddress
import socket
from typing import Dict, Any, Union
from pathlib import Path
from backend.mcp.builder.normalized_api_model import NormalizedAPIModel, NormalizedOperation, ParameterModel, RequestBodyModel, ServerModel

class OpenAPIParserError(Exception):
    pass

class SSRFViolationError(OpenAPIParserError):
    pass

class OpenAPIParser:
    def __init__(self):
        self.raw_spec = {}

    def parse_file(self, file_path: Union[str, Path]) -> NormalizedAPIModel:
        path = Path(file_path)
        if not path.exists():
            raise OpenAPIParserError(f"File not found: {file_path}")
        with open(path, "r", encoding="utf-8") as f:
            return self.parse_text(f.read(), hint=path.suffix)

    def parse_text(self, content: str, hint: str = "") -> NormalizedAPIModel:
        content = (content or "").strip()
        if not content:
            raise OpenAPIParserError("Empty OpenAPI specification")
        try:
            if hint in [".yaml", ".yml"] or (not content.startswith("{") and not content.startswith("[")):
                self.raw_spec = yaml.safe_load(content)
            else:
                self.raw_spec = json.loads(content)
        except Exception:
            try:
                self.raw_spec = yaml.safe_load(content)
            except Exception as e:
                raise OpenAPIParserError(f"Could not parse OpenAPI spec: {e}") from e
        if not isinstance(self.raw_spec, dict):
            raise OpenAPIParserError("OpenAPI spec must be an object")
        return self._normalize(self.raw_spec)
        
    # TEST-ONLY seam (default False in every deployed process): allows
    # numeric-loopback targets so the self-extension test loop can exercise
    # the full build/probe/execute cycle against a local server. Workflow
    # core.http keeps strict validation — a user workflow must never be able
    # to reach the app's own localhost (audit SSRF model).
    _LOOPBACK_TESTING = False

    def _validate_ssrf(self, url: str):
        """Reject non-public endpoints, including every IPv4/IPv6 DNS answer."""
        parsed = urllib.parse.urlparse(url or "")
        hostname = (parsed.hostname or "").rstrip(".").lower()
        loopback = hostname in ("127.0.0.1", "::1") and OpenAPIParser._LOOPBACK_TESTING
        if not loopback and (parsed.scheme.lower() != "https" or not parsed.hostname):
            raise SSRFViolationError("A complete HTTPS URL is required")
        if parsed.username is not None or parsed.password is not None:
            raise SSRFViolationError("Credentials in URLs are prohibited")
        if hostname == "metadata.google.internal" or hostname.endswith(".localhost") or hostname == "localhost":
            raise SSRFViolationError("Local and metadata hosts are prohibited")
        if loopback:
            return
        try:
            port = parsed.port or 443
            answers = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, ValueError) as exc:
            raise SSRFViolationError("Target must resolve to a public address") from exc
        if not answers:
            raise SSRFViolationError("Target must resolve to a public address")
        for answer in answers:
            ip = ipaddress.ip_address(answer[4][0].split("%")[0])
            if not ip.is_global or getattr(ip, "ipv4_mapped", None) is not None and not ip.ipv4_mapped.is_global:
                raise SSRFViolationError("Non-public network addresses are prohibited")

    def _resolve_ref(self, ref_str: str) -> Dict[str, Any]:
        """Resolves local #/components/... refs. Rejects remote refs."""
        if not ref_str.startswith("#/"):
            raise OpenAPIParserError(f"Remote $refs are prohibited: {ref_str}")
            
        parts = ref_str[2:].split("/")
        current = self.raw_spec
        for part in parts:
            part = part.replace("~1", "/").replace("~0", "~")
            if part not in current:
                raise OpenAPIParserError(f"Invalid $ref path: {ref_str}")
            current = current[part]
        return current
        
    def _deep_resolve(self, obj: Any, seen: frozenset = frozenset()) -> Any:
        if isinstance(obj, dict):
            if "$ref" in obj:
                ref = obj["$ref"]
                if ref in seen:
                    # Recursive shapes remain unconstrained at the cycle boundary.
                    return {k: v for k, v in obj.items() if k != "$ref"}
                resolved = self._resolve_ref(ref)
                return self._deep_resolve({**resolved, **{k: v for k, v in obj.items() if k != "$ref"}}, seen | {ref})
            return {k: self._deep_resolve(v, seen) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._deep_resolve(item, seen) for item in obj]
        return obj

    def _normalize(self, spec: Dict[str, Any]) -> NormalizedAPIModel:
        # 1. Resolve entire spec to flatten it
        spec = self._deep_resolve(spec)
        
        info = spec.get("info") if isinstance(spec.get("info"), dict) else {}
        servers_data = spec.get("servers") or []
        if not isinstance(servers_data, list):
            servers_data = []
        
        servers = []
        for s in servers_data:
            if not isinstance(s, dict) or not s.get("url"):
                continue
            url = str(s.get("url", ""))
            # Relative server URLs ("https://api.example.com/v1") are valid
            # OpenAPI 3 — keep them and let the builder resolve them against
            # the spec's own URL. Only absolute URLs are SSRF-checked here;
            # resolved results are checked before any request is made.
            if not url.startswith(("http://", "https://")):
                servers.append(ServerModel(url=url, description=s.get("description")))
                continue
            try:
                self._validate_ssrf(url)
                servers.append(ServerModel(url=url, description=s.get("description")))
            except Exception:
                continue
            
        components = spec.get("components") if isinstance(spec.get("components"), dict) else {}
        security_schemes = components.get("securitySchemes") if isinstance(components.get("securitySchemes"), dict) else {}
        global_security = spec.get("security") if isinstance(spec.get("security"), list) else []
        
        operations = []
        paths = spec.get("paths") or {}
        if not isinstance(paths, dict):
            paths = {}
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            shared_params = methods.get("parameters") or []
            if not isinstance(shared_params, list):
                shared_params = []
            for method, op_data in methods.items():
                if method.lower() not in ["get", "post", "put", "patch", "delete"]:
                    continue
                if not isinstance(op_data, dict):
                    continue

                parameters = []
                raw_params = list(shared_params) + list(op_data.get("parameters") or [])
                for p in raw_params:
                    if not isinstance(p, dict) or not p.get("name"):
                        continue
                    try:
                        parameters.append(ParameterModel(**p))
                    except Exception:
                        continue

                req_body = None
                rb_data = op_data.get("requestBody")
                if isinstance(rb_data, dict):
                    try:
                        req_body = RequestBodyModel(
                            description=rb_data.get("description"),
                            required=bool(rb_data.get("required", False)),
                            content=rb_data.get("content") or {},
                        )
                    except Exception:
                        req_body = None

                security = op_data.get("security", global_security) or []
                if not isinstance(security, list):
                    security = []

                op_servers = []
                for s in op_data.get("servers") or []:
                    if not isinstance(s, dict) or not s.get("url"):
                        continue
                    op_url = str(s.get("url", ""))
                    if not op_url.startswith(("http://", "https://")):
                        op_servers.append(ServerModel(url=op_url, description=s.get("description")))
                        continue
                    try:
                        self._validate_ssrf(op_url)
                        op_servers.append(ServerModel(**s))
                    except Exception:
                        continue

                op = NormalizedOperation(
                    operation_id=str(op_data.get("operationId") or f"{method}_{str(path).replace('/', '_').strip('_')}"),
                    http_method=method.upper(),
                    path=str(path),
                    summary=op_data.get("summary") if isinstance(op_data.get("summary"), str) else None,
                    description=op_data.get("description") if isinstance(op_data.get("description"), str) else None,
                    parameters=parameters,
                    request_body=req_body,
                    security_requirements=security,
                    servers=op_servers,
                )
                operations.append(op)
                
        return NormalizedAPIModel(
            info=info,
            servers=servers,
            operations=operations,
            security_schemes=security_schemes
        )
