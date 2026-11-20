# Sandtable local chat repair — 19 November 2026

This runbook describes the Sandtable repairs and how to validate them locally.

## Behavior

- The conversation fills the available screen height. Messages have their own visible scrollbar; the composer remains on screen. Reading older messages is no longer interrupted by refreshes. “Jump to latest” resumes following new replies.
- A submitted follow-up appears immediately as “Sending…”. Once accepted, its real run ID and original text stay in the thread while its saved result loads. Failed submissions retain the text and reuse the same submission identity on retry. The artificial 350 ms follow-up delay was removed.
- One shared stream reconciles saved run/task snapshots on events and reconnects. A watchdog covers silent disconnections and retries a failed final fetch. Terminal runs stop streaming after their history and final snapshot arrive. Events arriving during a read force another read before publication.
- The backend saves final run status before publishing the terminal event. Task outputs were already persisted; they are now reliably fetched by the chat. A completed run with no saved answer says so explicitly instead of displaying “Thinking…”. Failed, cancelled, expired, and review-required runs also have explicit terminal messages.
- Execution is now a compact, rounded upper-right card, sized to its content. Its header collapses the whole card; Activity, individual steps, Outputs, Sources, and Run details expand independently. Inline activity groups also expand below each user message. The card retains disclosure state when hidden, supports Escape and focus return, selects earlier messages, and links saved outputs back to the conversation. On narrow screens it floats above the chat without covering the composer. Graph, evaluation, command details, optional usage, and feedback load only when Run details is opened. The conversation no longer contains a budget card or a queue receipt after every answer.
- Context usage is a compact header button; details are fetched on demand. The full existing model catalog, grouped families, exact model IDs, disabled unavailable choices, and model help remain intact. Sandtable branding remains intact.
- Standalone packaging now copies static assets beside the server matching the current build, supporting both the nested repository layout and the flat Docker layout. The local test launcher selects that same build.

- Long dashboard pages render immediately without scroll-reveal blur. Decorative backgrounds and message entrance/pulsing animations are removed from the working dashboard. The start page has a clear New chat action, an optional tools guide, and editable suggestions.
- The composer grows to fit multiline text, preserves Shift+Enter, and restores saved drafts on first mount and refresh. Account and thread changes isolate drafts; accepted messages clear them. A restored submission identity is reused. Local storage failures are visible.
- The Firestore adapter now implements public catalog reads and publish/unpublish updates. Public reads use one equality filter, require explicit visibility, and sort locally, without needing a new composite index. Missing documents return false; database failures remain failures. Catalog loading errors offer retry and no longer also claim the catalog is empty.
- Settings loading failures offer retry. Integration errors remain inline, controls stay usable, mobile columns stack, and the page explains that enabled tools may still lack validation. The unsupported end-to-end encryption badge was removed.

The photographed existing website connector still needs valid definition/operation evidence before blocked operations can run. This change does not fabricate validation or alter live connector records. The catalog fix addresses a confirmed missing-method defect in local code; the live service was not queried to prove that this was its only error.

This removes client-side waiting and unnecessary reads. It does not claim to reduce Gemini inference time or Cloud Run cold-start time; those require later live measurements.

## Files changed in this task

- `frontend/src/app/dashboard/workspace/[run_id]/page.tsx`: conversation sizing/scrolling, accepted follow-ups, snapshot consumption, terminal states, optional right-hand execution panel.
- `frontend/src/app/globals.css`, `frontend/src/app/built.css`: responsive chat/sidebar/composer layout and compact model controls; generated CSS rebuilt.
- `frontend/src/components/ChatComposer.tsx`: immediate submission feedback, original goal on acceptance, typing while a run is active, IME-safe Enter handling, quieter character counter, auto-growing text input, restored drafts, editable suggestions.
- `frontend/src/components/ExecutionTimeline.tsx`: collapsed task and event groups with saved status and optional browser previews.
- `frontend/src/components/WorkspaceExecutionCard.tsx`: compact Codex-style card, nested disclosures, real outputs and safe source links.
- `frontend/src/components/ScrollRevealObserver.tsx`: application content excluded from reveal gating.
- `frontend/src/hooks/useDraft.ts`: first-mount restoration and account/thread isolation.
- `frontend/src/app/dashboard/layout.tsx`, `frontend/src/app/dashboard/page.tsx`: simpler start screen and quieter dashboard.
- `frontend/src/app/dashboard/catalog/page.tsx`, `frontend/src/app/dashboard/settings/page.tsx`, `frontend/src/app/dashboard/integrations/[mcp_id]/page.tsx`: honest load/error/retry states, readable content and responsive integration details.
- `backend/repositories/firestore/mcp_repository.py`: missing catalog repository methods.
- `frontend/tests/mounted/execution-card.test.tsx`, `frontend/tests/mounted/draft-restoration.test.tsx`, `frontend/e2e/dashboard-usability.spec.ts`, `backend/tests/test_firestore_catalog.py`: card, draft, dashboard and cloud-adapter regressions using local fixtures.
- `frontend/src/components/ModelSelector.tsx`: optional compact presentation; no catalog entries removed.
- `frontend/src/components/ContextUsage.tsx`: on-demand context details and Escape dismissal.
- `frontend/src/lib/run-subscriptions.ts`: coordinated snapshot reconciliation and terminal cleanup.
- `backend/engine/engine.py`: persist terminal state before emitting its event.
- `frontend/prepare-standalone.mjs`, `frontend/start-standalone.mjs`, `frontend/playwright.config.ts`: package and test the actual current production build.
- `frontend/tests/run-subscriptions.test.ts`, `frontend/e2e/sandtable-chat.spec.ts`, `backend/tests/test_chat_completion.py`: regression coverage.
- `deploy/cloud-run/sandtable.frontend.yaml`: unapplied future service template.
- This document: change record and release handoff.

## Automation, agents and Studio

In chat, “Create YT MCP tools” selects the YouTube website builder. “Create MCP tools for YouTube Music” selects the music site. For an existing website connector, **Run Test** adds and checks a fixed public start-page read. The build reply gives its actual tool name. The check covers the page title and HTTP status. It cannot validate playback, account actions or a free-form browser task. Generated actions retain separate eligibility checks. Retesting a disabled integration preserves its disabled state.

An enabled custom agent can be addressed by starting a chat message with its exact ID, for example `@yt-assistant Read the page title` if that ID exists in the account. The worker reads the current plugin record, enforces tenant ownership and checks the allowed tools. A child build requested by the agent carries the parent task and the operations it needs to resume.

Studio writes job and artifact records to the configured Firestore database. It can repair a partially generated batch and retry temporary failures reading a job's status. The Studio card shows terminal errors and identifies missing files. An interrupted generation can be started again after the job reports failure.

| Area | Changed files in the backend repair |
| --- | --- |
| Agent selection and execution | `backend/agents/agent_factory.py`, `backend/agents/orchestrator/orchestrator_agent.py`, `backend/api/main.py`, `backend/api/routers/workflows.py`, `backend/engine/direct_plan.py`, `backend/engine/engine.py`, `backend/worker.py` |
| MCP build and invocation | `backend/agents/mcp_factory/mcp_factory_agent.py`, `backend/api/routers/integrations.py`, `backend/mcp/openapi_executor.py`, `backend/mcp/tool_policy.py`, `backend/mcp/tool_router.py`, `backend/models/mcp_schemas.py` |
| Validation and storage | `backend/services/browser_connector_validation.py`, `backend/services/connector_activation.py`, `backend/services/connector_eligibility.py`, `backend/services/website_health.py`, `backend/repositories/base.py`, the SQLite and Firestore `mcp_repository.py` files, and the PostgreSQL `connector_contract.py` and `repositories.py` files |
| Studio and frontend | `backend/api/routers/capabilities.py`, `backend/services/artifact_builder.py`, `frontend/src/lib/api.ts`, and the dashboard pages for chat, agents, Studio and integration details |
| Regression coverage | `backend/tests/test_backend_feature_journeys.py`, `backend/tests/test_connector_build_service.py`, `backend/tests/test_readiness_regressions.py`, `backend/tests/test_phase26_connector_storage.py`, `frontend/e2e/dashboard-usability.spec.ts` |

## Future service name: sandtable

`deploy/cloud-run/sandtable.frontend.yaml` declares `metadata.name: sandtable`. Its image is intentionally an unresolved placeholder, so it cannot accidentally launch an untested image. No deployment step was added to `cloudbuild.frontend.yaml`.

Before any later deployment:

1. Resolve the final API origin and the service URL assigned to `sandtable` in `agentos-devpost` / `us-central1`. A frontend code edit cannot rename an existing Cloud Run URL; the future service must actually be named `sandtable`. Do not guess its generated hostname or silently redirect the existing candidate site.
2. Build with `_API_URL` pointing at the intended permanent API including `/api/v1`, `_SITE_URL` set to the final frontend origin, and `_GOOGLE_CLIENT_ID` when used. The existing Cloud Build file passes these into `NEXT_PUBLIC_*` build arguments. Setting them only at container runtime does not change the compiled browser bundle.
3. Replace the template's image placeholder with the tested immutable image digest. Cloud Run supplies `PORT=8080` for the template's container port; the standalone server honors it. Existing local Docker defaults remain unchanged.
4. Separately review API `FRONTEND_BASE_URL`, exact CORS origins, sign-in redirect/origin settings, and frontend access policy. Preserve the current service/traffic until the new service is validated. These are future infrastructure actions and were not performed here.

## Local release checks

Run the backend regression tests from the repository root. The connector storage tests also support a local Firestore emulator and a local test PostgreSQL instance.

```sh
python -m pytest backend/tests/test_{backend_feature_journeys,connector_build_service,readiness_regressions,firestore_catalog,shared_catalog,chat_completion,workflow_data_contracts,vertex_model_configuration,gemini_catalog,phase26_connector_storage,cloudbuild_substitutions}.py -q
cd frontend
npm ci
npm run lint -- --max-warnings=0
npm test
npm run build
E2E_PRODUCTION=1 npx playwright test e2e/sandtable-chat.spec.ts e2e/dashboard-usability.spec.ts --workers=1
```

Set `E2E_PORT` to a free local port if 3000 is occupied. The two browser suites use controlled API and event fixtures.
