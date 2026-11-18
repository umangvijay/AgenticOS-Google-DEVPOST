# ADR 006: MCP Sandbox

## Context
Executing dynamically generated Python code based on third-party APIs poses severe security risks (SSRF, credential theft, RCE).

## Decision
We will execute all dynamically generated MCP servers in a strictly isolated **Cloud Run Sandbox Container**.

## Rationale
- Containerization provides boundary enforcement at the OS level.
- Cloud Run allows strict limitation of CPU, Memory, and Network egress (via VPC Service Controls or Egress rules).

## Consequences
- Requires a separate, heavily restricted Service Account with zero project-level IAM roles.
- Requires building and pushing a sandbox Docker image.


## Update — Phase 7 runtime decision (2026-11-01)

This ADR originally described a general "Cloud Run Sandbox Container". The
Phase 7 implementation replaces the broad claim with the actual decision
record (`backend/services/runner_security.py::RUNTIME_DECISIONS`):

- **Selected first candidate: Cloud Run sandbox** — blocks outbound traffic
  by default and separates metadata access (§7.14/§7.15 support).
- **Documented constraint**: its default filesystem allows reading the
  parent container's root filesystem — therefore a MINIMAL dedicated runner
  image is REQUIRED and must contain no application secrets or customer
  files. CPU/memory are shared with the host container allocation.
- **Status: `selected_pending_proof`** — managed execution remains
  UNAVAILABLE until the backend passes the 10 executable isolation
  acceptance tests (`ISOLATION_ACCEPTANCE_TESTS`) in a live deployment
  (§7.5 step 5). gVisor is the recorded fallback candidate.
- **Trust boundaries (§7.6)**: control plane (authorization, artifacts,
  budgets) | builder (isolated dependency resolution) | runner supervisor
  (limits, observation, cleanup) | generated server (untrusted, narrowly
  granted) | outbound operation broker (capability-bound provider access —
  §7.14/§7.15). Generated code never receives DB admin credentials, broad
  service-account credentials, the AgentOS signing key, registry
  credentials, other users' files, container sockets, or arbitrary host
  mounts.
