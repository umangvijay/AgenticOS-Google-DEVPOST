# Architecture

AgentOS is a local-first autonomous workspace. The laptop default is SQLite + an in-process worker. Firestore / Pub/Sub / Cloud Run remain optional when `STORAGE_BACKEND=firestore`.

**Review boundary — 31 October 2026:** topology and component presence do not
establish deployed guarantees. See [implementation status](implementation-status.md)
and [the full-stack roadmap](full-stack-roadmap.md). Planned outbox commands,
atomic budget reservations, managed browser leases, verified ReAct records and
experiment services are phase contracts, not implied current infrastructure.

## Tagline

Tell it what you want. It builds the tools and does the work.

## Runtime

```mermaid
flowchart TD
  UI["Next.js workspace"] --> API["FastAPI and authorization"]
  API --> Engine["Workflow engine"]
  Engine --> Core["Core tasks"]
  Engine --> Agent["Orchestrator"]
  Agent --> Tools["Validated tools"]
  Agent --> Browser["Browser runtime"]
  Engine --> Store["State and events"]
  Store --> UI
```

Planner **is** the decomposer. There is no third LLM hop named Decomposer.

## Dynamic MCP (three paths)

1. Discover: does a registered tool match the goal?
2. If yes → use it (`pick_catalog_tool` scores name, MCP, and description against the follow-up text).
3. If no → MCP factory:

| Path | Planner method | Factory | Execution |
| --- | --- | --- | --- |
| OpenAPI / Swagger URL | `url` | Fetch, normalize, generate schemas, probe a safe GET | HTTP tools |
| HTTP API, no spec | `prompt` | Gemini spec **or** sketched OpenAPI against a known public host | HTTP tools |
| Website, no API | `website` | Playwright tool plan locked to origin | `runOnSite`, `login`, `openHome`, … |

Chat and **Integrations → Create** share factory components. Tools still need
validation, credentials, policy and runtime eligibility. The reviewed API and
orchestrator build entry points have different dispatch boundaries; roadmap
phase 04 specifies a common durable command lifecycle.

## Data stores (local)

| Store | Path / table |
| --- | --- |
| SQLite | `data/agentos.db` — users, runs, tasks, events, MCP registry, schedules |
| Vault ciphertext | secrets table, AES-256-GCM |
| Contact inbox and blogs | Backend content repository: SQLite records or Firestore collections; delivery status is separate from persistence |
| JWT keys | `backend/security/keys/*.pem` (gitignored) |

## Data flow

```mermaid
flowchart LR
  user[UserGoal] --> api[FastAPI]
  api --> sqlite[(SQLite)]
  api --> planner[PlannerDAG]
  planner --> catalog[MCPCatalog]
  catalog -->|miss| factory[MCPFactory]
  factory --> httpTools[HTTPTools]
  factory --> browserTools[WebsiteBrowserTools]
  httpTools --> exec[Executor]
  browserTools --> exec
  catalog -->|hit| exec
  exec --> events[SSETimeline]
  events --> ui[Workspace]
  exec -->|CAPTCHA_OTP_MFA| hitl[PauseHITL]
  hitl --> ui
```

## Self-healing

Retryable: timeouts, network, 429/quota. Semantic errors can enter RECOVERING when `recovery_enabled` (default on new tasks). Not retried: SSRF, 401/403, CAPTCHA/OTP/MFA (those pause).

Recovery policy must distinguish safe retries from uncertain external effects.
Advanced verified recovery, persistent evaluations, controlled self-improvement,
and contextual routing are described separately in roadmap phases 10–14. The
current adaptive model router does not train model weights.

## Optional cloud mode

Cloud Run + Secret Manager + Vertex AI is the first hosting target ([deploy-gcp.md](deploy-gcp.md)). SQLite files do not survive Cloud Run scale-to-zero; use Firestore for durable cloud data. Cloud Run Worker Pools are a different resource from HTTP services and are required for this repository's long-running Pub/Sub pull consumer.

## GCP deployment architecture

```mermaid
flowchart TB
  subgraph client[User-facing tier]
    browser[Browser]
    next[Next.js on Cloud Run]
    browser --> next
  end
  subgraph control[Request and control plane]
    api[FastAPI on Cloud Run\nRUN_WORKER=0]
    auth[JWT / CSRF / rate limits]
    approvals[Human approval checkpoints]
    api --> auth
  end
  subgraph data[Durable state and delivery]
    firestore[(Firestore)]
    pubsub[Pub/Sub topics + pull subscriptions]
  end
  subgraph execution[Background execution plane]
    pool[Cloud Run Worker Pool\npython -m backend.worker]
    planner[Planner / ReAct loop]
    mcp[MCP catalog and builder]
    browsertools[Playwright / HTTP tools]
    vertex[Gemini + embeddings on Vertex AI]
  end
  next -->|HTTPS REST, SSE| api
  api --> firestore
  api -->|enqueue| pubsub
  pubsub -->|pull, ack/nack| pool
  pool --> firestore
  pool --> planner
  planner --> vertex
  planner --> mcp
  mcp --> browsertools
  browsertools --> approvals
  approvals -->|resume| api
  pool -->|persist status/results| firestore
  api -->|SSE reads persisted events| next
```

Use distinct user-managed service accounts for API and worker. The API needs
Firestore access, Pub/Sub publish, Vertex AI inference, and access to the
runtime secrets it actually reads. The worker needs Firestore, Pub/Sub
subscription access, Vertex AI inference, and the same stable encryption/JWT
material required to process jobs. Avoid service-account key files; attach the
identity to each runtime and use Application Default Credentials.

## GCP run and data flow

```mermaid
sequenceDiagram
  actor User
  participant UI as Next.js
  participant API as Cloud Run API
  participant DB as Firestore
  participant Q as Pub/Sub
  participant W as Cloud Run Worker Pool
  participant V as Vertex AI Gemini
  participant T as MCP / HTTP / browser tools
  User->>UI: Outcome request
  UI->>API: Create run (authenticated, CSRF protected)
  API->>DB: Persist run and initial tasks
  API->>Q: Publish task event
  Q->>W: Deliver via pull subscription
  W->>DB: Claim task / check idempotency
  W->>V: Plan or choose next action (ADC)
  alt A required tool is missing
    W->>V: Generate candidate tool/schema from supplied public API docs
    W->>T: Probe safe read-only behavior and validate policy
    W->>DB: Register only after validation and applicable approval
  end
  W->>T: Execute allowed action
  alt consequential action needs approval
    W->>DB: Save WAITING_APPROVAL and pending action
    API-->>UI: Stream approval checkpoint
    User->>UI: Approve / reject
    UI->>API: Decision
    API->>Q: Resume event
  else normal result
    W->>DB: Save result, events, and run state
  end
  API-->>UI: SSE state/results
  UI-->>User: Activity and deliverable
```

Delivery is at-least-once, not exactly-once. Every external side effect needs a
stable idempotency key or an explicit duplicate-action policy. Model-generated
plans and tools are untrusted proposals: validate schemas, constrain outbound
hosts and methods, redact credentials, and require human approval for sensitive
or irreversible operations. A generated tool must never gain authority merely
because it was generated successfully.

## Portability boundary

The workflow engine should depend on repository, message-bus, model-provider,
and browser/tool interfaces rather than cloud SDKs. SQLite/in-memory adapters
are for local development; Firestore/Pub/Sub/Vertex are the current GCP
adapters. Before claiming multi-cloud portability, add contract tests for each
interface and implement/operate the alternate adapters (for example
PostgreSQL + a durable queue + another model provider). Configuration switches
alone do not provide portability.
