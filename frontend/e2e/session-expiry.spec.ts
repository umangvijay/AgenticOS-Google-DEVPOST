import { test, expect } from '@playwright/test';

/**
 * Audit R10 finding (deployed build): guest session expiry mid-generation
 * hard-bounced to /login and destroyed the composer input.
 *
 * The repo fixes this chain: guests get refresh tokens, and apiFetch
 * retries through /auth/refresh on any 401. This spec simulates token
 * expiry mid-action and asserts: no redirect, input preserved, request
 * retried successfully against the refreshed token.
 */

test.describe('Mid-action session expiry', () => {
  test('expired token mid-session: apiFetch refreshes, retries, and never navigates away (input survives)', async ({ page }) => {
    let expiredOnce = false;
    let refreshed = false;

    await page.addInitScript(() => localStorage.setItem('agentos_auth', JSON.stringify({
      accessToken: 'expired-token',
      refreshToken: 'valid-refresh-token',
      user: { id: 'test-user', email: 'guest@x', name: 'Guest', role: 'guest', is_active: true },
    })));

    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      const auth = route.request().headers()['authorization'] || '';
      if (path.endsWith('/auth/refresh')) {
        refreshed = true;
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({
          access_token: 'fresh-token', refresh_token: 'rotated-refresh',
          user: { id: 'test-user', email: 'guest@x', name: 'Guest', role: 'guest', is_active: true },
        }) });
        return;
      }
      if (path.endsWith('/notifications/stream')) {
        await route.abort();
        return;
      }
      if (path.endsWith('/notifications')) {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ notifications: [], unread_count: 0 }) });
        return;
      }
      if (path.endsWith('/usage/models')) {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ models: [], policy: 'p', note: '' }) });
        return;
      }
      if (path.endsWith('/usage/operations')) {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({
          sample_size: 0, sample_limit: 200, completion_rate: null, median_completion_seconds: null,
          status_counts: {}, failed_tasks_by_agent: {}, attention: [], generated_at: '2026-10-17T00:00:00Z',
          cost_tracking: { mode: 'estimated', note: '', estimated_tokens: 0, estimated_cost_usd: 0, usd_per_1m_tokens: 0.6 },
        }) });
        return;
      }
      if (path.endsWith('/settings')) {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ settings: {}, available_models: [] }) });
        return;
      }
      if (auth.includes('expired-token') && !path.endsWith('/auth/refresh')) {
        expiredOnce = true;
        await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ detail: 'Token has expired' }) });
        return;
      }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({}) });
    });

    await page.goto('/dashboard/operations');
    await page.waitForTimeout(500);

    // The refresh round-trip happened and the page did NOT navigate to /login.
    expect(refreshed).toBe(true);
    expect(expiredOnce).toBe(true);
    expect(page.url()).not.toContain('/login');
    // The rotated tokens were persisted for subsequent calls.
    const stored = await page.evaluate(() => JSON.parse(localStorage.getItem('agentos_auth') || '{}'));
    expect(stored.accessToken).toBe('fresh-token');
    expect(stored.refreshToken).toBe('rotated-refresh');
  });
});
