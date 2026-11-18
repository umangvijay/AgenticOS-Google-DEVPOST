# Provider authentication (§20.7–§20.9)

AgentOS talks to model providers through ONE adapter interface
(`backend/providers/base.py`: `ProviderAdapter`), with per-user,
per-invocation configuration — never process-global credentials.

## The adapters

| Provider | Kind | Auth shape | Notes |
|---|---|---|---|
| Gemini Developer API | `GEMINI_DEV` | API key (`?key=`) | default shared model path |
| Vertex AI | `VERTEX_AI` | **express API key** (`?key=` endpoint) **or** standard ADC (project + location endpoint) | the two modes are NOT interchangeable — verified by tests |
| Bedrock | `BEDROCK` | `AWS_BEARER_TOKEN_BEDROCK` bearer header or the boto3 credential chain | Converse API; tools as `toolUse`/`toolResult` blocks |
| Ollama | `OLLAMA` | none (local server) | §20.7 local inference; capabilities declare no image input |

Each adapter: `validate_credentials()` (real endpoint probe, never
printing the secret), `generate()` (normalized
messages/usage/tool-calls), `generate_stream()` (SSE), and
`normalize_*_error()` mapping provider errors to ONE `ErrorCategory`
vocabulary (rate_limit, auth, invalid_request, server, network,
overloaded) that the retry/recovery policy consumes.

## Per-user configuration is isolated (§20.9)

Provider config travels with the REQUEST (`ProviderConfig` instances
per user/invocation, resolved by `backend/services/llm_context.py`);
mutating one user's config cannot affect another's (tested). Keys are
stored in the vault and loaded at dispatch time; they never land in
logs, spans, or error messages (telemetry sanitizer).

## Adding a key locally

1. Settings → API keys (or `POST /api/v1/settings/keys`): the key is
   encrypted into the vault.
2. Health reports `llm_status` honestly: `"missing"` until a usable
   credential exists — the UI shows the same state; no demo fallback
   fires.
3. Verify without spending: each adapter's `validate_credentials()`
   probes the provider endpoint (`python -m
   backend.providers.gemini_dev validate` style entry points in
   `backend/providers/__init__.py::create_adapter`).

## Live acceptance (credential-gated)

The request-shaping and error-normalization contracts run without
credentials (`pytest backend/tests/test_phase20_providers.py -q`).
LIVE inference through Vertex/Bedrock additionally needs real
credentials and is marked as such in the evidence matrix — the shape
tests are local, the live call is the credential gate.
