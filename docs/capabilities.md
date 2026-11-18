# Capabilities and supported scope

Updated 31 October 2026. “Present” describes source functionality, not a deployed
acceptance result. Use [release evidence](RELEASE_EVIDENCE.md) for verification and
[the roadmap](full-stack-roadmap.md) for planned extensions.

| Capability | Present foundation | Boundary / next gate |
|---|---|---|
| API adapters | OpenAPI normalization, generated definitions, read probes | Per-operation auth/write tests; accurate revision-bound coverage |
| Inferred integrations | Documentation/description-derived definitions | Provenance and independent validation; no any-app guarantee |
| Browser automation | Playwright observe/act, credential placeholders, challenge pause | Cleanup, durable session ownership, action approvals and supported DOM journeys |
| Workflow execution | DAGs, events, leases, retries, parent/build records | Unified queued builds, cloud crash/replay/cancellation tests |
| Recovery | Bounded repair and uncertain quarantine | Provider reconciliation and verified recovery outcomes |
| Model routing | UCB-style ranking and exploration | Verified rewards; contextual routing is planned |
| Memory | User-scoped storage and embedding search | Adversarial isolation, lifecycle and retrieval evaluation |
| Resume | Parsing/tailoring, heuristic ATS analysis, HTML output | Field provenance and rendered/output acceptance |
| Studio | Health checks, non-executing debug, generated files | Generated code needs its declared build/runtime checks |
| Shared catalog | Integration publication and user installs | Revision evidence, revocation and no secret transfer |
| Organizations | Roles, invitations and run ownership metadata | All-path acceptance and transaction/resource isolation |
| UI and 3D | React workspace, DAG, timeline, landing scene | Accessibility, real-backend journeys, adaptive performance |
| Cloud | Firestore/Pub/Sub/Cloud Run configuration | Actual candidate deployment and operational drills |

## Planned extensions

Advanced ReAct verification, expanded DOM actions, managed browser sessions,
self-improvement experiments, contextual bandits, specialized subagents,
provenance-backed artifacts, optional 3D workflow exploration, templates, and
enterprise options have phase-specific gates. They are not all shipped features.

## Generated deliverables

Compact, standard and full generation options describe requested output scope,
not assurance that an application is deployable. Files are user-owned; generated
code is not automatically safe to execute. A supported deliverable must pass its
format, factuality, integrity, authorization and rendering/build checks.

Website tools are UI automation, not undisclosed official APIs. Credentials and
authorization remain necessary. CAPTCHA/OTP/MFA require human participation.
