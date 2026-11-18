# Settings & Model API Keys

- **One key field for any provider** — Gemini, Claude, Grok, Z.ai, OpenAI. Every key is validated live against the provider *before* being stored (AES-256-GCM in Vault). Gemini powers the agent; Grok is the automatic quota fallback.
- **Autonomy level** 0–3 controls how much the agent may do without asking.
- **Theme** switches instantly (true dark glass theme).
- Security: *"Protected by industry-standard AES-256 and end-to-end client-side encryption"* — and it's true: secrets are encrypted in-browser (Web Crypto) and again at rest.

## API keys (programmatic access)

JWT sessions expire in 15 minutes by design. For scripts, CI, and MCP clients, create an API key: **Settings → API Keys → Create key**.

- Keys look like `vry_…`; the raw value is shown **once** — only a hash is stored server-side.
- Use them anywhere a Bearer token goes: `Authorization: Bearer vry_…`
- They carry your role and scoping, so a key can do anything you can.
- Revoke from the same table; revocation takes effect on the next request. Last-used timestamps make stale keys visible.

## Run webhooks (outbound)

Settings → Run Webhooks: set a public HTTPS URL and every finished run (COMPLETED or FAILED) POSTs a signed summary — headers `x-agentos-timestamp` and `x-agentos-signature` (`sha256=` HMAC over `{timestamp}.{body}`). Verify signatures before trusting the payload; optionally store a Vault secret named `webhook_out` to override the default signing key.

## Shared integration catalog

The catalog (`/dashboard/catalog`) lists integrations published by other users. Publishing is gated: owner-only, and only **verified** integrations (OpenAPI builds whose live probe passed) — the catalog shows metadata and tool names only, never the spec, the owner's identity, or credentials. **Install** clones the integration into your own workspace; auth *placement* carries over, the publisher's secret never does — attach your own key via the Integrations page or the Vault.
