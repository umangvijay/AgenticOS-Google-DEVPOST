/**
 * Phase 20 §20.12: real-backend browser journeys.
 *
 * Playwright drives the REAL frontend (webServer: npm run dev:fast)
 * against the REAL local backend (uvicorn on :8000, sqlite storage).
 * Nothing is routed/mocked: signup is a real guest session, the goal is
 * really submitted through the chat composer, the engine really
 * executes it, and the workspace shows the run's real output.
 *
 * Requires the local backend:
 *   .venv/bin/python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8000
 * (or `docker compose --profile core up api`). Without it the suite
 * reports the block LOUDLY instead of faking a pass.
 */
import { test, expect } from '@playwright/test';

const BACKEND = 'http://127.0.0.1:8000';
const GOAL = 'Check the health of https://example.com';

let backendUp = false;
let token = '';

test.beforeAll(async ({ request }) => {
  try {
    const health = await request.get(`${BACKEND}/health`, { timeout: 5000 });
    backendUp = health.ok();
    if (!backendUp) return;
    // A REAL guest session on the REAL backend.
    const res = await request.post(`${BACKEND}/api/v1/auth/guest`, { timeout: 10000 });
    const body = await res.json();
    token = body.access_token || '';
  } catch {
    backendUp = false;
  }
});

test('backend reachability is reported loudly', async () => {
  // This test ALWAYS runs: it documents the gate state honestly. When
  // the backend is down it FAILS with the runnable command — the other
  // journeys then skip with the same reason.
  const runnable = '.venv/bin/python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8000 (or docker compose --profile core up api)';
  expect(backendUp, `BLOCKED: no local backend on :8000 — run: ${runnable}. The journey is NOT faked.`).toBe(true);
});

test.describe.serial('real-backend journeys', () => {
  test.use({ storageState: undefined });

  test('guest session submits a goal through the composer, the run completes, and the workspace shows the real output', async ({ page, request }) => {
    test.skip(!backendUp, 'BLOCKED: no local backend on :8000 — see the reachability test');
    // The engine plans with REAL local inference: on a warm-up run this
    // exceeds playwright's default 30 s test timeout, which would kill
    // the 120 s element wait below before the run could complete.
    test.setTimeout(180_000);

    await page.addInitScript(([tok]) => {
      localStorage.setItem('agentos_auth', JSON.stringify({
        accessToken: tok,
        refreshToken: tok,
        user: { id: 'e2e-local', email: 'e2e-local@example.invalid', name: 'E2E Local', role: 'user', is_active: true },
      }));
    }, [token] as const);

    await page.goto('/dashboard');
    // THE REAL COMPOSER: type a real goal and submit it.
    const composer = page.getByPlaceholder('Ask anything. Sandtable will plan, build missing tools, and do the work.');
    await expect(composer).toBeVisible({ timeout: 30000 });
    await composer.fill(GOAL);
    await page.getByLabel('Send').click();

    // The composer navigates to the real workspace view for the run.
    await page.waitForURL(/\/dashboard\/workspace\//, { timeout: 30000 });
    const runId = page.url().split('/').pop() || '';
    expect(runId).not.toBe('');

    // The workspace reflects the REAL run reaching COMPLETED.
    await expect(
      page.getByText('COMPLETED', { exact: false }).first(),
    ).toBeVisible({ timeout: 120000 });

    // Cross-check through the REAL API with the SAME identity: the run
    // completed with REAL output data about the real target.
    const res = await request.get(`${BACKEND}/api/v1/workflows/${runId}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.ok()).toBeTruthy();
    const run = await res.json();
    expect(run.status).toBe('COMPLETED');
    const task = run.tasks?.[0];
    expect(task?.status).toBe('COMPLETED');
    expect(JSON.stringify(task?.output_data)).toContain('example.com');
  });
});
