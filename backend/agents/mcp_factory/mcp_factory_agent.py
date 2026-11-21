import hashlib
import json
import logging
from html.parser import HTMLParser
from typing import Any, Dict, Optional

import httpx

from urllib.parse import urljoin, urlparse

from backend.agents.mcp_factory.code_generator import CodeGenerator
from backend.agents.mcp_factory.tool_registry_manager import ToolRegistryManager
from backend.engine.mcp_catalog import detect_named_apps, resolve_named_spec, sketch_openapi_from_prompt
from backend.mcp.builder.openapi_parser import OpenAPIParser, OpenAPIParserError, SSRFViolationError
from backend.mcp.builder.schema_generator import SchemaGenerator
from backend.mcp.openapi_executor import execute_openapi_tool
from backend.mcp.website_mcp import (
    default_start_url,
    looks_like_website_without_api,
    origin_from_url,
    plan_website_connector,
    tools_from_plan,
    website_mcp_id,
)
from backend.services import gemini_client
from backend.services.gemini_client import GeminiQuotaExceeded, is_quota_error

logger = logging.getLogger(__name__)


class SpecLinks(HTMLParser):
    """Look for explicit specification links on the supplied documentation page."""
    def __init__(self, source):
        super().__init__()
        self.source = source
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag not in {"a", "link"} or len(self.links) >= 5:
            return
        href = dict(attrs).get("href")
        if not href:
            return
        target = urljoin(self.source, href)
        parsed = urlparse(target)
        if (parsed.scheme == "https" and parsed.netloc == urlparse(self.source).netloc
                and parsed.path.lower().endswith((".json", ".yaml", ".yml"))
                and target not in self.links):
            self.links.append(target)


def auth_metadata_from_spec(model, fallback_type: str = "API_KEY") -> Dict[str, str]:
    """Derive credential placement from the spec's securitySchemes so the
    executor sends the secret where the API actually reads it
    (e.g. X-API-Key header vs Authorization: Bearer vs ?api_key=)."""
    schemes = getattr(model, "security_schemes", None) or {}
    if not schemes and not any(op.security_requirements for op in getattr(model, "operations", [])):
        return {"type": "NONE"}
    for scheme in (schemes or {}).values():
        if not isinstance(scheme, dict):
            continue
        stype = str(scheme.get("type") or "").lower()
        if stype == "http" and str(scheme.get("scheme") or "").lower() == "bearer":
            return {"type": "BEARER"}
        if stype == "http" and str(scheme.get("scheme") or "").lower() == "basic":
            return {"type": "BASIC"}
        if stype == "apikey" or stype == "apiKey":
            location = str(scheme.get("in") or "header").lower()
            name = str(scheme.get("name") or "").strip()
            if location == "query" and name:
                return {"type": "API_KEY", "in": "query", "name": name}
            if name:
                return {"type": "API_KEY", "in": "header", "name": name}
            return {"type": "API_KEY", "in": "header"}
    return {"type": str(fallback_type or "API_KEY").upper()}


class MCPFactoryAgent:
    def __init__(self, mcp_repo, secrets_repo=None):
        self.mcp_repo = mcp_repo
        self.secrets_repo = secrets_repo
        self.code_gen = CodeGenerator()
        self.registry = ToolRegistryManager(mcp_repo)
        self.parser = OpenAPIParser()
        self.schema_gen = SchemaGenerator()

    async def _log(self, build_id: Optional[str], message: str, logs: list) -> None:
        logs.append(message)
        logger.info("[mcp-factory] %s", message)
        if build_id:
            await self.mcp_repo.update_build(build_id, {"logs": logs, "stage": logs[-1][:80]})

    async def run_build(
        self,
        *,
        user_id: str,
        method: str,
        source: str,
        name: str = "",
        build_id: Optional[str] = None,
        auth_type: str = "API_KEY",
        intent: str = "",
    ) -> Dict[str, Any]:
        logs: list = []
        try:
            if build_id:
                await self.mcp_repo.update_build(build_id, {"status": "running", "stage": "fetching"})
            await self._log(build_id, "Starting MCP factory", logs)
            if method == "python_tools":
                from backend.services.standalone_build import build_compute_connector
                return await build_compute_connector(self.mcp_repo, user_id=user_id,
                    source=source, name=name, build_id=build_id)
            if method == "website" or (
                method in ("prompt", "url")
                and looks_like_website_without_api(source, url=source if str(source).startswith("http") else None)
            ):
                return await self._build_website(user_id=user_id, source=source, name=name, build_id=build_id, logs=logs)

            spec_text, source_uri = await self._ingest(method, source, logs, build_id, name=name)
            await self._log(build_id, "Parsing OpenAPI specification", logs)
            model = self.parser.parse_text(spec_text)
            if not model.operations:
                raise ValueError("No HTTP operations found in the specification")
            if not model.servers:
                for op in model.operations:
                    if op.servers:
                        model.servers = list(op.servers)
                        break
            # Relative server URLs must be resolved against the spec's own URL
            # BEFORE deciding the spec is unusable — "/api/v3" alone is common.
            self._resolve_relative_servers(model, source_uri)
            if not model.servers:
                raise ValueError(
                    "OpenAPI spec has no usable HTTPS server URLs. If it uses relative "
                    "server URLs, build from the spec's URL (or paste the spec) so they can be resolved."
                )
            # §29.14: the intake record reaches its RETRIEVED stage as soon
            # as the interface is documented — what the spec actually
            # declares, before any generation runs.
            if build_id:
                try:
                    from backend.services.app_intake import (
                        finalize_intake_record, documented_operations_from_model,
                        authentication_requirements_from_auth,
                    )
                    _stored = await self.mcp_repo.get_build(build_id)
                    _intake = dict((_stored or {}).get("intake") or {})
                    if _intake:
                        _spec_digest = hashlib.sha256(
                            json.dumps(self.parser.raw_spec, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")
                        ).hexdigest()
                        _finalized = finalize_intake_record(
                            _intake, source_uri=source_uri,
                            spec_digest=_spec_digest,
                            documented_operations=documented_operations_from_model(model),
                            authentication_requirements=authentication_requirements_from_auth(
                                getattr(model, "auth", None)
                                or (getattr(model, "info", {}) or {}).get("auth") or {}),
                            unresolved_assumptions=[
                                "relative server URLs were resolved against the "
                                "spec source" if source_uri else
                                "no retrieval source URI recorded",
                            ])
                        await self.mcp_repo.update_build(
                            build_id, {"intake": _finalized})
                except Exception:
                    await self._log(build_id, "Intake record finalization skipped", logs)

            spec_hash = hashlib.sha256(json.dumps(self.parser.raw_spec, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            spec_version = str(model.info.get("version") or "1.0.0")
            display_name = name or model.info.get("title") or "Custom Integration"
            mcp_id = hashlib.sha256(f"{user_id}:{spec_hash}".encode("utf-8")).hexdigest()[:16]

            existing = await self.mcp_repo.get_mcp(mcp_id)
            if existing and existing.get("owner") == user_id and existing.get("is_enabled"):
                cached = await self.mcp_repo.get_cached_tools(mcp_id)
                if cached:
                    if intent:
                        selected = self.schema_gen.generate(model, mcp_id, spec_version, intent=intent)
                        combined = {t["tool_name"]: t for t in cached}
                        for selected_tool in selected:
                            combined.setdefault(selected_tool.tool_name, selected_tool.model_dump(mode="json"))
                        cached = list(combined.values())
                        await self.mcp_repo.cache_tools(mcp_id, cached)
                    summaries = [{"name": t.get("tool_name"), "description": t.get("description", "")} for t in cached]
                    await self._log(build_id, "Reusing the registered specification and credentials", logs)
                    if build_id:
                        await self.mcp_repo.update_build(build_id, {"status": "success", "stage": "reused", "mcp_id": mcp_id, "tools": summaries, "logs": logs})
                    return {"status": "success", "reused": True, "mcp_id": mcp_id, "tools": summaries,
                            "message": f"Reusing {existing.get('name') or display_name}", "name": existing.get("name") or display_name}

            # Spec provenance (audit G06; Phase 6 §6.4): where the operation
            # definitions came from determines the trust ceiling — an inferred
            # sketch can never earn the verified tier, whatever a probe says.
            # Unknown import methods default to `unknown`, never official_spec.
            from backend.services.tool_validation import spec_source_provenance
            spec_provenance = spec_source_provenance(method)
            total_operations = len(model.operations)
            await self._log(build_id, f"Generating tool schemas ({min(total_operations, 30)} ops)", logs)
            tools = self.schema_gen.generate(model, mcp_id, spec_version, intent=intent,
                                             spec_provenance=spec_provenance)
            if not tools:
                raise ValueError("Schema generator produced no tools")

            await self._log(build_id, "Validating generated tool metadata", logs)
            for t in tools:
                if not t.operation or not (t.operation.get("servers") or t.operation.get("path")):
                    raise ValueError(f"Tool {t.tool_name} is missing operation metadata")

            auth_meta = auth_metadata_from_spec(model, fallback_type=auth_type)
            # §29.14/§29.15 revision: the persisted Forge record — the
            # product identity of this build (resolved URI, raw vs
            # normalized digests, kind, per-operation definition digests).
            if build_id:
                try:
                    from backend.services.app_intake import (
                        forge_record, operations_forged, NORMALIZER_VERSION,
                    )
                    _tools_json = [t.model_dump(mode="json") for t in tools]
                    await self.mcp_repo.update_build(build_id, {
                        "intake": {**(await self.mcp_repo.get_build(build_id))
                                   .get("intake", {}),
                                   "forge": forge_record(
                                       kind="openapi_adapter", method=method,
                                       source_uri=source_uri,
                                       raw_digest=hashlib.sha256(
                                           (spec_text or "").encode("utf-8")
                                       ).hexdigest(),
                                       effective_spec_digest=spec_hash,
                                       normalizer_version=NORMALIZER_VERSION,
                                       operations=operations_forged(
                                           _tools_json, spec_hash),
                                       spec_provenance=spec_provenance.value
                                       if hasattr(spec_provenance, "value")
                                       else str(spec_provenance))}})
                except Exception:
                    await self._log(build_id, "Forge record persistence skipped", logs)
            from backend.services.tool_validation import credential_binding_id_from
            probe_binding = credential_binding_id_from({"type": "NONE"})
            probed_tool = None
            await self._log(build_id, "Running live sandbox probe", logs)
            probe = {"status": "skipped", "reason": "No read-only tool with complete probe inputs"}
            for candidate in tools:
                if (candidate.operation or {}).get("http_method") == "GET" and not candidate.input_schema.get("required"):
                    probe = await self._probe(candidate.model_dump(mode="json"), model)
                    if probe.get("ok") is True:
                        from backend.services.tool_validation import record_read_probe
                        candidate.operation = record_read_probe(candidate.model_dump(mode="json"), spec_hash, probe_binding)["operation"]
                        probed_tool = candidate
                    break
            await self._log(build_id, f"Probe result: {probe.get('status', probe)}", logs)

            # Self-healing codegen (audit §9): a crashed probe on a model-built
            # spec means the generated integration is broken, not the API. Feed
            # the error back once and rebuild before registering.
            if probe.get("status") == "probe_error" and method == "prompt" and source_uri is None:
                try:
                    await self._log(build_id, "Probe crashed; regenerating the spec with the error fed back", logs)
                    healed_source = (
                        f"{source}\n\nThe previously generated integration crashed at runtime: "
                        f"{str(probe.get('error'))[:400]}. Produce a simpler, correct OpenAPI 3 spec."
                    )
                    healed_spec = await self._spec_from_prompt(healed_source, logs, build_id)
                    healed_model = self.parser.parse_text(healed_spec)
                    if healed_model.operations:
                        if not healed_model.servers:
                            for op in healed_model.operations:
                                if op.servers:
                                    healed_model.servers = list(op.servers)
                                    break
                        self._resolve_relative_servers(healed_model, None)
                        if healed_model.servers:
                            healed_hash = hashlib.sha256(json.dumps(self.parser.raw_spec, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                            model = healed_model
                            spec_hash = healed_hash
                            spec_version = str(model.info.get("version") or "1.0.0")
                            mcp_id = hashlib.sha256(f"{user_id}:{spec_hash}".encode("utf-8")).hexdigest()[:16]
                            tools = self.schema_gen.generate(model, mcp_id, spec_version, intent=intent,
                                                             spec_provenance=spec_provenance)
                            probe = {"status": "skipped", "reason": "regenerated spec"}
                            for candidate in tools:
                                if (candidate.operation or {}).get("http_method") == "GET" and not candidate.input_schema.get("required"):
                                    probe = await self._probe(candidate.model_dump(mode="json"), model)
                                    break
                            await self._log(build_id, f"Rebuilt spec; probe result: {probe.get('status', probe)}", logs)
                except Exception as heal_exc:
                    await self._log(build_id, f"Self-heal regeneration failed ({heal_exc}); registering the original build", logs)

            trust_tier = "verified" if method in ("url", "spec", "openapi") and probe.get("ok") is True else "pending_review"
            # A registered definition is not executable until its operation evidence passes policy.
            auto_enable = trust_tier == "verified" and credential_binding_id_from(auth_meta) == probe_binding

            # Prefer the spec's own security scheme placement; fall back to the requested auth_type.
            auth_meta = auth_metadata_from_spec(model, fallback_type=auth_type)

            # Per-operation test coverage (audit G02 / v5.6 A9; Phase 6 §6.7/
            # §6.12): digest-verified against this revision's spec hash and
            # bound to the credential CONTEXT (ref+version+epoch), not the
            # raw ref. No credential context → historical coverage only.
            from backend.services.tool_validation import (
                coverage_from_tools, credential_binding_id_from,
            )
            test_coverage = coverage_from_tools(
                tools, spec_hash=spec_hash,
                credential_binding_id=credential_binding_id_from(auth_meta),
            )
            capabilities_omitted = max(0, total_operations - len(tools))
            await self._log(
                build_id,
                f"Coverage: {test_coverage['read_probe_passed']}/{test_coverage['total']} operations "
                f"probe-tested ({test_coverage['mutating_untested']} untested writes); "
                f"provenance={spec_provenance}; {capabilities_omitted} capability(ies) not selected",
                logs,
            )

            await self._log(build_id, "Registering MCP in the catalog", logs)
            mcp_id = await self.registry.register_mcp(
                display_name,
                f"Auto-generated from {method}",
                tools,
                owner=user_id,
                trust_tier=trust_tier,
                source_type=method,
                source_uri=source_uri,
                spec_json=spec_text,
                spec_hash=spec_hash,
                spec_version=spec_version,
                auth=auth_meta,
                is_enabled=auto_enable,
                mcp_id=mcp_id,
                spec_provenance=spec_provenance,
                test_coverage=test_coverage,
                capabilities_omitted=capabilities_omitted,
            )

            if auto_enable and probed_tool is not None:
                from backend.services.connector_build_validation import persist_build_probe
                _parent_run = None
                try:
                    if build_id:
                        _build_record = await self.mcp_repo.get_build(build_id)
                        _parent_run = (_build_record or {}).get("parent_run_id")
                except Exception:
                    _parent_run = None
                await persist_build_probe(self.mcp_repo, mcp_id,
                                          probed_tool.model_dump(mode="json"),
                                          probe, owner_user_id=user_id,
                                          parent_run_id=_parent_run)

            tool_summaries = [{"name": t.tool_name, "description": t.description} for t in tools]
            names = ", ".join(t["name"] for t in tool_summaries[:16] if t.get("name"))
            needs_auth = str(auth_meta.get("type") or "").upper() not in ("", "NONE")
            auth_hint = (
                "\nThis API authenticates"
                + (f" via {auth_meta.get('name') or 'Authorization'}"
                   + (" query parameter" if auth_meta.get("in") == "query" else " header")
                   if auth_meta.get("name") else " with a Bearer token")
                + ". Attach its API key on the integration page (or Vault) before calling tools."
                if needs_auth else ""
            )
            probe_warning = ""
            if probe.get("status") in ("probe_error",) or probe.get("ok") is False:
                probe_warning = f"\nLive probe did not succeed ({probe.get('http_status') or probe.get('error') or 'error'}) — verify the API is reachable and the spec's server URL is correct."
            gate_warning = ""
            if not auto_enable:
                gate_warning = (
                    "\nThe integration is registered DISABLED pending validation. "
                    "Connect credentials, run validation, then enable the tested operations."
                )
            msg = (
                f"{'Built' if auto_enable else 'Prepared a disabled draft for'} {display_name} with {len(tools)} tool{'s' if len(tools) != 1 else ''}"
                + (f": {names}." if names else ".")
                + f"\nmcp_id: {mcp_id}."
                + auth_hint
                + probe_warning
                + gate_warning
            )
            result = {
                "status": "success",
                "message": msg,
                "reply": msg,
                "mcp_id": mcp_id,
                "name": display_name,
                "tools": tool_summaries,
                "trust_tier": trust_tier,
                "total_operations": len(model.operations),
                "selected_operations": len(tools),
                "selection_method": "keyword_relevance" if intent else "specification_order",
            }
            if build_id:
                await self.mcp_repo.update_build(build_id, {
                    "status": "success",
                    "stage": "registered",
                    "mcp_id": mcp_id,
                    "tools": tool_summaries,
                    "logs": logs,
                    "error": None,
                })
            return result
        except Exception as e:
            logger.exception("MCP build failed")
            if build_id:
                await self.mcp_repo.update_build(build_id, {
                    "status": "error",
                    "stage": "failed",
                    "error": str(e),
                    "logs": logs + [f"FAILED: {e}"],
                })
            return {"status": "error", "message": str(e)}

    async def _build_website(
        self,
        *,
        user_id: str,
        source: str,
        name: str,
        build_id: Optional[str],
        logs: list,
    ) -> Dict[str, Any]:
        start = default_start_url(source, "https://example.com")
        if "example.com" in start and "http" not in (source or "").lower():
            raise ValueError("Name the website URL so tools can open it in the browser.")
        origin = origin_from_url(start)
        await self._log(build_id, f"No OpenAPI — building browser tools for {origin}", logs)
        plan = await plan_website_connector(source, origin, start)
        display = name or str(plan.get("name") or urlparse(origin).hostname)
        mcp_id = website_mcp_id(user_id, origin)
        tools = tools_from_plan(mcp_id, origin, start, plan)
        await self._log(build_id, f"Registering {len(tools)} browser tools", logs)
        spec = json.dumps({"origin": origin, "start_url": start, "plan": plan}, default=str)
        spec_hash = hashlib.sha256(spec.encode("utf-8")).hexdigest()
        mcp_id = await self.registry.register_mcp(
            display,
            f"Browser tools for {origin} (no OpenAPI — Playwright)",
            tools,
            owner=user_id,
            trust_tier="pending_review",
            source_type="website",
            source_uri=start,
            spec_json=spec,
            spec_hash=spec_hash,
            spec_version="1.0.0",
            auth={"type": "NONE"},
            is_enabled=True,
            mcp_id=mcp_id,
            spec_provenance="inferred_description",
        )
        from backend.services.browser_connector_validation import validate_browser_connector
        manifest = await self.mcp_repo.get_mcp(mcp_id)
        cached = await self.mcp_repo.get_cached_tools(mcp_id)
        await self._log(build_id, "Checking browser start pages (task actions remain approval-gated)", logs)
        check = await validate_browser_connector(self.mcp_repo, manifest, cached, user_id)
        cached = await self.mcp_repo.get_cached_tools(mcp_id)
        summaries = [{"name": t["tool_name"], "description": t.get("description", "")} for t in cached]
        msg = (f"Built {display}. Use {check['tool_name']} to read the public page title and status on {origin}. "
               "Other proposed actions need their own validation before use.")
        result = {
            "status": "success",
            "message": msg,
            "reply": msg,
            "mcp_id": mcp_id,
            "name": display,
            "tools": summaries,
            "trust_tier": "browser_ready",
            "kind": "website",
        }
        if build_id:
            await self.mcp_repo.update_build(build_id, {
                "status": "success",
                "stage": "registered",
                "mcp_id": mcp_id,
                "tools": summaries,
                "logs": logs,
                "error": None,
            })
        return result

    async def build_from_url(self, url: str, name: str = "API Integration", user_id: str = "system") -> Dict[str, Any]:
        return await self.run_build(user_id=user_id, method="url", source=url, name=name)

    async def build_from_prompt(self, prompt: str, name: str = "Custom Integration", user_id: str = "system") -> Dict[str, Any]:
        return await self.run_build(user_id=user_id, method="prompt", source=prompt, name=name)

    async def build_from_spec(self, spec: str, name: str = "API Integration", user_id: str = "system") -> Dict[str, Any]:
        return await self.run_build(user_id=user_id, method="spec", source=spec, name=name)

    @staticmethod
    def _resolve_relative_servers(model, source_uri: Optional[str]) -> None:
        """OpenAPI allows relative server URLs; resolve them against the spec's own URL."""
        from urllib.parse import urljoin
        from backend.mcp.builder.openapi_parser import OpenAPIParser

        ssrf = OpenAPIParser()._validate_ssrf

        def fix(servers):
            for server in servers:
                if server.url.startswith(("http://", "https://")):
                    continue
                if source_uri:
                    resolved = urljoin(source_uri, server.url)
                    # A scheme-relative or crafty relative URL must not escape
                    # the spec's own origin — validate like any absolute URL.
                    ssrf(resolved)
                    server.url = resolved
                else:
                    raise ValueError(
                        f"Spec uses relative server URL '{server.url}' but no source URL is known"
                    )

        fix(model.servers)
        for op in model.operations:
            fix(op.servers)

    async def _fetch_text(self, url: str) -> str:
        import asyncio
        headers = {"User-Agent": "AgentOS/1.0 (+OpenAPI integration builder)",
                   "Accept": "application/json, application/yaml, text/yaml, text/html;q=0.8"}
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False, headers=headers) as client:
            for _ in range(6):
                self.parser._validate_ssrf(url)
                for attempt in range(3):
                    async with client.stream("GET", url) as response:
                        if response.status_code in (429, 502, 503, 504) and attempt < 2:
                            await asyncio.sleep(0.5 * (2 ** attempt))
                            continue
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError("Documentation redirect has no destination")
                            url = urljoin(url, location)
                            break
                        response.raise_for_status()
                        chunks = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 20 * 1024 * 1024:
                                raise ValueError("Specification exceeds the 20 MB limit")
                            chunks.append(chunk)
                        return b"".join(chunks).decode("utf-8-sig")
            raise ValueError("Too many documentation redirects")

    def _parse_or_none(self, content: str):
        try:
            model = self.parser.parse_text(content)
            return content if model.operations and ("openapi" in self.parser.raw_spec or "swagger" in self.parser.raw_spec) else None
        except Exception:
            return None

    async def _try_well_known_specs(self, source: str, logs: list, build_id: Optional[str], content: str = "") -> Optional[tuple]:
        parsed = urlparse(source)
        if not parsed.scheme or not parsed.netloc:
            return None
        base = f"{parsed.scheme}://{parsed.netloc}"
        links = SpecLinks(source)
        links.feed(content[:100_000])
        candidates = links.links
        for path in (
            "/openapi.json",
            "/openapi.yaml",
            "/swagger.json",
            "/swagger.yaml",
            "/v3/api-docs",
            "/api-docs",
            "/api/openapi.json",
        ):
            url = urljoin(base + "/", path.lstrip("/"))
            if url.rstrip("/") != source.rstrip("/"):
                candidates.append(url)
        for url in dict.fromkeys(candidates):
            try:
                await self._log(build_id, f"Trying spec at {url}", logs)
                content = await self._fetch_text(url)
                if self._parse_or_none(content):
                    return content, url
            except Exception:
                continue
        return None

    async def _spec_from_prompt(self, source: str, logs: list, build_id: Optional[str]) -> str:
        prompt = (
            "You convert a natural-language description of ANY application or HTTP API "
            "into a compact OpenAPI 3.0 JSON object. Named apps (GitHub, Slack, Notion, "
            "Stripe, Gmail, Shopify, HubSpot, Jira, etc.) must use their official public HTTPS base URL. "
            "Include only the operations needed for the request, typically 4–12. "
            "Each path operation needs operationId, summary, and responses. "
            "servers must be a non-empty array of https URLs. No localhost, metadata, or invented hosts.\n\n"
            "Return JSON only with keys: openapi, info, servers, paths.\n\nDescription:\n" + source
        )
        spec = await gemini_client.generate_json(prompt)
        if isinstance(spec, dict):
            spec = json.dumps(spec)
        return spec

    async def _ingest(self, method: str, source: str, logs: list, build_id: Optional[str], name: str = "") -> tuple:
        if method in ("spec", "openapi"):
            return source, None
        if method == "prompt":
            await self._log(build_id, "Asking the model to produce an OpenAPI 3 spec for this app", logs)
            last_error: Optional[Exception] = None
            for attempt in range(2):
                try:
                    spec = await self._spec_from_prompt(source, logs, build_id)
                    if self._parse_or_none(spec):
                        return spec, None
                    last_error = ValueError("Model spec did not parse as OpenAPI 3")
                    await self._log(build_id, "Model spec was invalid; retrying once", logs)
                    source = source + "\n\nThe previous JSON was invalid OpenAPI 3. Return a simpler valid spec."
                except (GeminiQuotaExceeded, Exception) as e:
                    last_error = e
                    if not (isinstance(e, GeminiQuotaExceeded) or is_quota_error(e)):
                        if attempt == 0:
                            await self._log(build_id, f"Model spec failed ({e}); retrying", logs)
                            continue
                        break
                    break
            named = resolve_named_spec(name) if name else None
            if named is None and len(detect_named_apps(source or "")) == 1:
                named = resolve_named_spec(source or "")
            if named:
                spec_text, display, _auth = named
                await self._log(
                    build_id,
                    f"Model unavailable or invalid spec; using bundled OpenAPI for {display}",
                    logs,
                )
                if spec_text.startswith("http"):
                    return await self._fetch_text(spec_text), spec_text
                return spec_text, None
            sketched = sketch_openapi_from_prompt(source or "")
            if sketched and self._parse_or_none(sketched):
                await self._log(
                    build_id,
                    "Model unavailable; sketched OpenAPI from the request (operations come from your wording)",
                    logs,
                )
                return sketched, None
            if last_error and (isinstance(last_error, GeminiQuotaExceeded) or is_quota_error(last_error)):
                raise ValueError(
                    "The model could not build this MCP (quota). Configure a supported LLM provider in Settings, "
                    "or paste an OpenAPI URL."
                ) from last_error
            raise ValueError(
                "The model could not produce a valid OpenAPI spec for that app. "
                "Paste an OpenAPI URL or a shorter description of the HTTP API."
            ) from last_error
        if method == "url":
            await self._log(build_id, f"Fetching {source}", logs)
            try:
                content = await self._fetch_text(source)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                content = ""
            parsed = self._parse_or_none(content) if content else None
            if parsed:
                return parsed, source
            found = await self._try_well_known_specs(source, logs, build_id, content)
            if found:
                return found
            raise ValueError(
                "I could reach the URL but could not find a usable OpenAPI definition. "
                "Send the provider’s OpenAPI JSON/YAML link, paste the definition, "
                "or request a website tool to read the public start page."
            )
        raise ValueError(f"Unknown ingest method: {method}")

    async def _probe(self, tool: Dict[str, Any], model) -> Dict[str, Any]:
        """Live list/schema check. Optional GET with no required params hits the real API."""
        op = tool.get("operation") or {}
        if op.get("http_method") != "GET":
            return {"status": "skipped", "reason": "first tool is not GET"}
        required = (tool.get("input_schema") or {}).get("required") or []
        if required:
            return {"status": "skipped", "reason": "tool requires arguments"}
        fake_manifest = {"auth": {"type": "NONE"}}
        try:
            result = await execute_openapi_tool(fake_manifest, tool, {}, user_id="system")
            return {"status": "probed", "ok": result.get("ok"), "http_status": result.get("status")}
        except SSRFViolationError as e:
            raise
        except Exception as e:
            return {"status": "probe_error", "error": str(e)}
