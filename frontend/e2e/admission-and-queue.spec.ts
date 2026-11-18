import { expect, test, Page } from "@playwright/test";

/**
 * Admission & queue E2E (Phase 3, §3.19).
 *
 * Black-box checks of the user-facing overload behavior:
 * - A rate-limited submission shows the precise AdmissionNotice state with
 *   the backend's retry timing — the draft is PRESERVED for resubmission.
 * - An admission outage (503 ADMISSION_UNAVAILABLE) is shown as "can't
 *   safely start new work", never as the user's quota being exhausted.
 * - A successful submission clears any stale admission notice.
 * - The workspace shows accepted work via QueueStatusPanel, backed by the
 *   real run id — never an invented queue position.
 *
 * Hermetic: the authenticated session and every API response are stubbed at
 * the route layer (same pattern as budget-controls.spec.ts); no backend or
 * provider is contacted.
 */

type AdmissionBody = {
  code: string;
  message: string;
  group?: string;
  retry_after_seconds?: number;
  accepted: false;
  enforcement?: string;
  decision_id?: string;
};

const state: { admissionPayload?: AdmissionBody | { detail: AdmissionBody }; acceptedRun?: boolean } = {};

const WORKFLOW = {
  run_id: "run-e2e-123",
  workflow_id: "wf-e2e-123",
  status: "PENDING",
  task_count: 2,
  thread_id: "thread-e2e",
};

const RUN = {
  run_id: "run-e2e-123",
  workflow_id: "wf-e2e-123",
  status: "PENDING",
  goal: "e2e goal",
  tasks: [
    { task_id: "root-1", workflow_id: "wf-e2e-123", run_id: "run-e2e-123",
      user_id: "e2e-user", agent: "OrchestratorAgent", status: "PENDING",
      dependencies: [], input_data: {}, output_data: null },
  ],
};

async function installStubs(page: Page) {
  await page.addInitScript(() => localStorage.setItem('agentos_auth', JSON.stringify({
    accessToken: 'test-token',
    refreshToken: 'test-refresh-token',
    user: { id: 'e2e-user', email: 'e2e@example.com', name: 'E2E', role: 'user', is_active: true },
  })));
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (path.endsWith('/csrf-token')) {
      return route.fulfill({ json: { csrf_token: 'test-csrf' } });
    }
    if (path.endsWith('/workflows') && method === 'POST') {
      if (state.acceptedRun) {
        return route.fulfill({ json: WORKFLOW, status: 201 });
      }
      const body = state.admissionPayload ?? { detail: {
        code: 'RATE_LIMITED',
        message: 'Too many requests in the current interval.',
        group: 'workflow_create',
        retry_after_seconds: 37,
        accepted: false,
        enforcement: 'shared',
        decision_id: 'dec-e2e-1',
      } as AdmissionBody };
      return route.fulfill({ json: body, status: 429 });
    }
    if (path.includes('/workflows/run-e2e-123')) {
      return route.fulfill({ json: RUN });
    }
    if (path.endsWith('/workflows') && method === 'GET') {
      return route.fulfill({ json: { workflows: [RUN], count: 1 } });
    }
    if (path.includes('/events')) {
      // SSE stub: fetch-event-source tolerates a plain 200 with no events.
      return route.fulfill({ status: 200, body: '' });
    }
    if (path.endsWith('/settings')) {
      return route.fulfill({ json: { settings: {}, available_models: [] } });
    }
    if (path.includes('/notifications')) {
      return route.fulfill({ json: { notifications: [], unread: 0 } });
    }
    if (path.includes('/orgs')) {
      return route.fulfill({ json: { orgs: [], count: 0 } });
    }
    if (path.includes('/integrations')) {
      return route.fulfill({ json: { integrations: [], count: 0 } });
    }
    return route.fulfill({ json: { ok: true } });
  });
}

test.beforeEach(async ({ page }) => {
  state.admissionPayload = undefined;
  state.acceptedRun = false;
  await installStubs(page);
});

test.describe("admission & queue states", () => {
  test("rate-limited submission shows the overload state with retry timing and preserves the draft", async ({ page }) => {
    await page.goto("/dashboard");
    const composer = page.locator("form.chat-composer");
    await expect(composer).toBeVisible();

    await composer.locator("textarea").fill("Build me a workflow for pokemon data");
    await composer.locator("button[type=submit]").click();

    const notice = page.getByRole("alert");
    await expect(notice).toBeVisible();
    await expect(notice.getByText("Too many requests")).toBeVisible();
    // Real backend timing is shown — 37s from the stubbed decision body.
    await expect(notice.getByText(/retry in ~37s/i)).toBeVisible();

    // §3.19: the user's draft survives a refusal for resubmission.
    await expect(composer.locator("textarea")).toHaveValue(
      "Build me a workflow for pokemon data");
  });

  test("admission outage shows 'cannot safely start' — never a quota message", async ({ page }) => {
    state.admissionPayload = { detail: {
      code: "ADMISSION_UNAVAILABLE",
      message: "AgentOS cannot safely decide whether to accept work right now.",
      group: "workflow_create",
      retry_after_seconds: 5,
      accepted: false,
      enforcement: "local_emergency",
      decision_id: "dec-e2e-2",
    } };
    await page.goto("/dashboard");
    const composer = page.locator("form.chat-composer");
    await composer.locator("textarea").fill("another goal");
    await composer.locator("button[type=submit]").click();

    const notice = page.getByRole("alert");
    await expect(notice).toBeVisible();
    await expect(notice.getByText("Can't safely start new work")).toBeVisible();
    await expect(notice.getByText(/rate limit/i)).toHaveCount(0);
  });

  test("retry from the notice submits again and clears the state on success", async ({ page }) => {
    // retry_after_seconds: 0 → the notice offers the Retry action immediately.
    state.admissionPayload = { detail: {
      code: "RATE_LIMITED",
      message: "Too many requests in the current interval.",
      group: "workflow_create",
      retry_after_seconds: 0,
      accepted: false,
      enforcement: "shared",
      decision_id: "dec-e2e-3",
    } };
    await page.goto("/dashboard");
    const composer = page.locator("form.chat-composer");
    await composer.locator("textarea").fill("retry me");
    await composer.locator("button[type=submit]").click();
    await expect(page.getByRole("alert")).toBeVisible();

    // Next POST succeeds: the Retry button resubmits the SAME draft.
    state.acceptedRun = true;
    await page.getByRole("alert").getByRole("button", { name: "Retry" }).click();

    // Workspace navigation happened (run accepted) — the rate-limit notice
    // is gone (other components may legitimately use role="alert", so the
    // assertion is scoped to the notice's content, not the global count).
    await expect(page).toHaveURL(/workspace/);
    await expect(page.getByText("Too many requests")).toHaveCount(0);
  });

  test("accepted work shows a queue state backed by the real run id", async ({ page }) => {
    await page.goto("/dashboard/workspace/run-e2e-123");
    const panel = page.getByTestId("queue-status-panel");
    await expect(panel).toBeVisible();
    await expect(panel).toHaveAttribute("data-state", "queued");
    await expect(panel.getByText("Queued")).toBeVisible();
    // The identifier is the REAL run id from the backend.
    await expect(panel.getByText("run-e2e-123")).toBeVisible();
    // No invented queue positions anywhere in the panel.
    await expect(panel.getByText(/position \d+/i)).toHaveCount(0);
  });
});
