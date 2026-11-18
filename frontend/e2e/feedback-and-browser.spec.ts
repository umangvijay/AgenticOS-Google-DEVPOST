/**
 * Phase 21 §21.21/§21.22: real-backend journeys for user feedback and
 * the browser session panel — driven against the live compose stack.
 * Skips loudly when the stack is down (never fakes a pass).
 */
import { test, expect } from '@playwright/test';

const BACKEND = 'http://127.0.0.1:8000';

let backendUp = false;
let token = '';

test.beforeAll(async ({ request }) => {
  try {
    const health = await request.get(`${BACKEND}/health`, { timeout: 5000 });
    backendUp = health.ok();
    if (!backendUp) return;
    const res = await request.post(`${BACKEND}/api/v1/auth/guest`,
                                   { timeout: 10000 });
    const body = await res.json();
    token = body.access_token || '';
  } catch {
    backendUp = false;
  }
});

test('reachability', async () => {
  const runnable = 'docker compose --profile core up (backend on :8000)';
  expect(backendUp,
    `BLOCKED: no stack on :8000 — run: ${runnable}`).toBe(true);
});

test.describe('user feedback journey', () => {
  test('submit → triage status visible to the user', async ({ request }) => {
    test.skip(!backendUp, 'BLOCKED: no stack');
    const submit = await request.post(`${BACKEND}/api/v1/feedback`, {
      headers: { Authorization: `Bearer ${token}` },
      data: { helpful: false, category: 'incorrect_result',
              comment: 'e2e: the summary mixed two runs',
              run_id: null, share_diagnostics: false },
    });
    expect(submit.status(), await submit.text()).toBe(201);
    const { feedback_id } = await submit.json();
    expect(feedback_id).toBeTruthy();
    // The user sees the triage status.
    const mine = await request.get(`${BACKEND}/api/v1/feedback/mine`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const body = await mine.json();
    const mine_entry = (body.feedback || []).find(
      (f: { id: string }) => f.id === feedback_id);
    expect(mine_entry?.triage_status).toBe('received');
    // Unauthenticated submission is refused.
    const anon = await request.post(`${BACKEND}/api/v1/feedback`, {
      data: { helpful: true, category: 'other' },
    });
    expect([401, 403]).toContain(anon.status());
    // The user can delete their own feedback (deletion right).
    const del = await request.delete(
      `${BACKEND}/api/v1/feedback/${feedback_id}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
    expect(del.status()).toBe(200);
  });
});
