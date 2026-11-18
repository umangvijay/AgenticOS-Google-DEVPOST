# MCP and connector creation

AgentOS can create candidate tool definitions from OpenAPI, API descriptions,
and supported website workflows. A URL or a registered manifest does not prove
working authenticated access. See [roadmap phases 04–09](full-stack-roadmap.md).

## Products

| Product | Current boundary |
|---|---|
| Declarative API adapter | Primary internal mapping/executor path; only validated permitted operations should execute |
| Website workflow | Playwright automation with separate session, DOM, credential and action-policy requirements |
| Standalone server | Packaging helpers exist; trusted isolated execution and content-bound protocol evidence remain gates |

## Lifecycle

1. Resolve an existing permitted capability before creating a duplicate.
2. Ingest a bounded source and preserve official/extracted/inferred provenance.
3. Generate candidate operations and explicitly report omitted capabilities.
4. Attach scoped credentials without placing secrets in definitions or prompts.
5. Validate each supported operation against its revision and credential context.
6. Activate only eligible operations through deterministic runtime policy.
7. Execute with approval, budget, ownership, idempotency and runtime checks.
8. Verify the result and deliver an authorized artifact or explicit unresolved state.

This is the target contract. The API dispatches builds through workflow work;
the reviewed orchestrator path records metadata but still executes its build
inline. A common record is not equivalent to a common durable execution boundary.

## Validation and evidence

A passing read probe establishes only its tested operation and context. It does
not verify every endpoint, authentication scheme, pagination or write operation.
Pending-review and invalidated definitions must remain unavailable. Displayed
coverage must use the same predicate as execution; the focused review found
stale evidence could still count in the coverage helper.

Add tests for schema/parameter encoding, authorization denial, authenticated
reads, pagination, empty results, malformed responses, timeouts and rate limits.
Writes require controlled accounts/fixtures and appropriate action approval.
Credential rotation or relevant definition changes invalidate affected evidence.

## Standalone execution

The legacy host tester deliberately returns unsupported. Packaging must use exact
resolved dependencies and immutable executable-content digests. Actual protocol
tests must run in the supported isolated runner and issue digest-bound evidence.
Manually recorded passing status flags do not establish protocol interoperability.
The reviewed deployment-status helper did not reject post-test content changes.

## User experience

Build, credential attachment, validation and activation are distinct states.
Provide a durable build link, reconnect/retest for partial success, operation
coverage, provenance and unsupported-capability explanations. Stopping browser
observation does not cancel backend work; cancellation needs an explicit command.

Browser tools need scoped hosts, action approvals, session ownership and tested
DOM targeting. Authentication challenges require a human in the correct session.
An inferred API or website description is never a guarantee of supported access.
