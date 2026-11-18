# Security Findings Register (Phase 22 §22.33)

Every release-blocking finding below was found by EXECUTED checks during
this qualification (live-stack probes, adversarial suites, dependency
audit) and re-tested after remediation. A scan output alone was never
accepted as evidence.

## Release-blocking findings — RESOLVED

| ID | Severity | Finding | How found | Fix | Re-test |
|---|---|---|---|---|---|
| S1 | **critical** | Logged-out access tokens kept working until TTL on the postgres profile: `PostgresTokenDenylistRepository` lacked `revoke_jti`, so logout's denylist write failed silently and `get_current_user` found nothing revoked | LIVE probe B5.3 (`scripts/qualify_live_probes.py`): logout → same token still authorized (200) | `revoke_jti` implemented (hash-only contract) + regression test `test_token_denylist_revoke_jti_contract` | probe re-run: revoked token → **401**; suite 12/12 |
| S2 | high | `pyjwt 2.13.0`: 14 published CVEs (advisories PYSEC-2026-41xx, GHSA-pxh4-856f-4h89) in the AUTH CORE | `pip-audit` on the frozen requirements | pinned `pyjwt==2.15.0`; auth suites re-run | 19/19 auth suites pass on 2.15.0 |
| S3 | high | `authlib 1.7.2`: 9 published CVEs | pip-audit | pinned `authlib==1.9.1` | pip check clean; auth suites pass |
| S4 | medium | Org invitation API 500 on the postgres profile: pg repo signature diverged from the shared invitation service (raw-token storage, missing expiry/email-match, wrong accept contract) | LIVE probe B5.4 (500 on invite) | contract-aligned `create/accept/resend/list/get_invitation*` + migration `0016` (hash-only, expiry) | probe re-run: invite **201**; 49/49 org suites |
| S5 | medium | Workflow-generated MCP tools invisible to their owner on postgres (ownership columns diverged: `owner` written, `user_id` filtered) | LIVE: integrations_list returned 0 for the owner after a successful build | ownership mirroring in `register_mcp` + parity filter in `list_mcps` + regression test | live re-run: owner sees GitHub (45 tools) |
| S6 | medium | Website→MCP planner bypassed ordinary provider dispatch (hardcoded hosted Gemini → honest ADC failure with no local path) | LIVE: chat_website_mcp FAILED | `_ollama_is_only_configured_provider` branch through the METERED ADK path | live re-run: website build **COMPLETED** via local Ollama |
| S7 | medium | `budget_reservations` pg schema diverged from the §2 contract (minimal 0001 shape vs runtime INSERT; NULL id; missing double-settle/dispatch-version/uncertain-subset semantics) | LIVE: website build FAILED on reservation NULL id; state-machine smoke found 4 divergences | migration `0015` + state-machine contract fixes | full §2 smoke passes; regression test `test_budget_state_machine_full_contract` (12/12) |

| S8 | medium | `_MeteredModel` wrapper fails the ADK's `isinstance(model, BaseLlm)` check → `LLMRegistry.new_llm` does `':' in model` on the wrapper → worker `TypeError` crash on model-object agents (found by a live worker crash after rebuild) | live worker logs | `__class__` delegation on the wrapper (reports the inner class to isinstance) + regression test in `test_phase18_batch_a.py` | 18/18 phase-18 suite; worker rebuilt |

## Verified protections (executed, not assumed)

| Boundary | Check | Evidence |
|---|---|---|
| SSRF (tool generation) | generated builds reject non-public/metadata targets | worker log shows live `SSRFViolationError: Non-public network addresses are prohibited`; `openapi_parser` requires public HTTPS |
| Generated-code isolation | 10 hostile fixtures + resource caps | `test_phase21_isolated_runner.py` 12/12 (secret read, cross-tenant, metadata, egress, raw socket, OOM, fork, output cap, non-cooperative cancel, descendants) |
| Cross-tenant access | live probes: read/cancel/thread on another user's run → 403 | `data/phase22_live_probes.json` B5.2 |
| Credential exposure | vault stores names/fields only; responses never echo values; `sk-probe-` marker absent from listings and evidence | B6.1 probes; marker scan of evidence artifacts clean |
| Uncertain external writes | dispatch never repeated on uncertainty | `test_phase21_injected_healing.py::uncertain_outcome` |
| Approval binding | argument-change/expiry refusal | approval suites (release-scope journey 6) |
| Browser ownership fencing | stale controller epoch refused server-side | `test_phase21_browser_takeover.py` 5/5 + DEPLOYED journey 1/1 |
| Effect idempotency | duplicate digest refused | effect ledger suites |
| Prompt injection (retrieved content) | browser/tool content treated as untrusted data | `test_phase14_browser_authority.py`, dom-targeting suites |

## Residual risks (accepted, with disposition)

| ID | Risk | Disposition |
|---|---|---|
| R1 | `opentelemetry-api 1.42.1` CVE-2026-114603 — pinned by `google-adk==2.7.1` (<1.45) | accepted: the API package's CVE is in collector-export paths this deployment does not enable; revisit when google-adk bumps. Owner: platform. |
| R2 | NATS pull-fetch inside the colima container network hangs (environment limitation, not an app defect; host-side NATS + durable Postgres outbox proven) | accepted with documented limitation; Phase 23 target: real VPS/k8s network |
| R3 | Firestore parity verified against the local EMULATOR only | accepted: real-Firestore project qualification is a Phase 23 target |
| R4 | Secrets scan covers logs/evidence/artifacts; no static SAST pass beyond the suite | accepted for RC: SAST (bandit/semgrep) scheduled as Phase 23 hardening |

## Marker scan (22.6)

`grep -r "sk-probe-\|DO-NOT-PRINT"` across `data/`, `docs/`, evidence
JSON: only the probe script itself and its redacted outputs (names
only) contain markers — no secret values persisted.

## Phase 30 defensive review additions (14 Nov)

Reviewed the newly extended authority surfaces with controlled fixtures
through the normal product APIs (no new findings required code rollback;
two hardening changes landed with their own regressions):

| ID | Surface | Control verified | Regression receipt |
|---|---|---|---|
| 30.6-A | Named operator actions | `OPERATOR_ACTIONS` denies unknown actions; plain admin cannot administer keys or restore backups; org roles never confer operator rights | `backend/tests/test_phase30_envelope_and_authority.py::test_operator_actions_are_named_and_separated` |
| 30.6-B | Support receipts | `redact_text` strips Authorization/Bearer/api-key/token/secret/password values and `gcp://` references from support-visible receipts | same file, `test_support_receipts_are_redacted` |
| 30.7-A | env1 envelope binding | wrong tenant, wrong purpose, wrong credential, wrong wrapping key and tampered ciphertext are all refused BEFORE plaintext exists (AEAD associated data binds tenant/credential/purpose/format) | same file, envelope tests |
| 30.7-B | Managed-secret versioning | stored references are version-qualified (`gcp://name@version`); upstream rotation cannot silently change what a record reads | same file, `test_managed_reference_parsing_and_pinned_read` |
| 30.7-C | Backfill CAS | a concurrent credential rotation between read and write is classified `conflict`, never overwritten | same file, `test_backfill_classifies_concurrent_conflict_and_corrupt` |
| 30.38-A | Preview cache partition | cache entries are principal-partitioned; invalidation is scoped to the caller; private descriptors remain never-cached | `backend/tests/test_phase30_preview_partition.py` + retained 28.7a cross-account refusal |
| 30.32-A | Rate-limit display | only actual provider observations surface (`provider_header`/`provider_receipt`/`provider_error`); invented sources are refused; absent data renders unavailable, never a guessed reset | `backend/tests/test_phase30_usage_ledger.py::test_provider_rate_limit_surface_and_absence` |
| 30.32-B | Money never silently zero | unknown model/tariff yields explicit `unavailable_reason`; pricing is versioned (`tariff-2026-11`) | same file, `test_pricing_is_explicit_when_unknown` |
| 30.45-A | Marker scan | synthetic-secret scan of new evidence/audit payloads (operator receipts redact; envelope records store counts only) | envelope + operator tests; restore drill emits digests only |

The restore drill now proves BOTH envelope generations decrypt after
restore and a wrong-tenant open of an env1 record is refused
(`scripts/qualify_restore_application.py`: checks
`restored_both_envelope_generations`, `restored_wrong_tenant_refused`).
