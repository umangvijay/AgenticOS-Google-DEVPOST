# MCP Forge — Build Tools for Any App

When an app has no connected tools, AgentOS *builds them*. Four input paths:

1. **OpenAPI URL / pasted spec** — deterministic: every operation becomes a typed tool. Verified fastest path.
2. **Docs URL (no OpenAPI)** — AgentOS fetches the docs, tries well-known spec locations, then has the model construct the spec from the page, and live-probes it.
3. **App description** — *"Build MCP tools for AcmeCRM so I can list deals"* — the model writes the spec, the sandbox probe validates it.
4. **Website (no API at all)** — see Website Tools.

**Using your new tools:** the build message lists tool names and the `mcp_id`. If the API needs a key, attach it on the integration page (or save it in Vault, then say *"use my <name> credential"*). Then just ask in chat: *"list the first 5 pokemon"* — the orchestrator calls your tools.

**Trust tiers:** `verified` (built from a real spec URL, live probe passed) vs `pending_review` (model-generated — review before production use).

**Self-healing:** failed calls return structured reasons (auth invalid, rate-limited, wrong-id) and the agent adjusts — e.g. lists resources first to discover valid ids.
