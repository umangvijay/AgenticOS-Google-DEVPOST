import { test, expect } from '@playwright/test';

/**
 * E2E for Schedules and Vault (Vault) pages — the audit's L2/L3 UI flows,
 * mocked at the API boundary like the other specs.
 */

const authInit = () => localStorage.setItem('agentos_auth', JSON.stringify({
  accessToken: 'test-only-token', refreshToken: 'r',
  user: { id: 'test-user', email: 't@example.invalid', name: 'T', role: 'user', is_active: true },
}));

test.describe('Schedules page', () => {
  test('shows an ACTIVE schedule with next run and toggles pause by status', async ({ page }) => {
    const schedule = {
      schedule_id: 'sch-1', name: 'watchdog', goal: 'check the health of https://example.com',
      cron_expression: '0 */2 * * *', timezone: 'UTC', status: 'ACTIVE', is_enabled: true,
      next_run_at: '2026-10-17T20:00:00+00:00', created_at: '2026-10-17T00:00:00Z',
    };
    await page.addInitScript(authInit);
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith('/schedules') && route.request().method() === 'GET') {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ schedules: [schedule], count: 1 }) });
        return;
      }
      if (path.endsWith('/pause') && route.request().method() === 'POST') {
        schedule.status = 'PAUSED'; schedule.is_enabled = false;
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ schedule_id: 'sch-1', status: 'PAUSED', is_enabled: false }) });
        return;
      }
      if (path.endsWith('/notifications')) {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ notifications: [], unread_count: 0 }) });
        return;
      }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({}) });
    });

    await page.goto('/dashboard/schedules');
    // Status contract (audit Round 3 fix): ACTIVE schedule shows Active, never "Paused"
    await expect(page.locator('.badge-success').getByText('Active')).toBeVisible();
    await expect(page.getByText('0 */2 * * *')).toBeVisible();
    // Cron-correct next run rendered from the API value (audit R2.6.1)
    await expect(page.getByText(/Next run:/)).toBeVisible();

    await page.getByTitle('Pause').click();
    await expect(page.locator('.badge-neutral').getByText('Paused')).toBeVisible();
  });

  test('run-now surfaces the returned run id', async ({ page }) => {
    const schedule = {
      schedule_id: 'sch-2', name: 'nightly', goal: 'g', cron_expression: '0 3 * * *',
      timezone: 'UTC', status: 'ACTIVE', is_enabled: true,
      next_run_at: '2026-10-18T03:00:00+00:00', created_at: '2026-10-17T00:00:00Z',
    };
    await page.addInitScript(authInit);
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith('/schedules') && route.request().method() === 'GET') {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ schedules: [schedule], count: 1 }) });
        return;
      }
      if (path.endsWith('/run') && route.request().method() === 'POST') {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({
          schedule_id: 'sch-2', run_id: 'sch-abc123', status: 'PENDING', message: 'Schedule execution triggered.', goal: 'g',
        }) });
        return;
      }
      if (path.endsWith('/notifications')) {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ notifications: [], unread_count: 0 }) });
        return;
      }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({}) });
    });

    await page.goto('/dashboard/schedules');
    await page.getByTitle('Run Now').click();
    // Round 3 fix: run-now returns the run id and the UI surfaces it (observability)
    await expect(page.getByText(/Run started: sch-abc123/)).toBeVisible();
  });
});

test.describe('Vault (credentials) page', () => {
  test('saves a credential and lists names only — raw value never rendered', async ({ page }) => {
    const stored: { name: string }[] = [];
    await page.addInitScript(authInit);
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      const method = route.request().method();
      if (path.endsWith('/credentials') && method === 'GET') {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ credentials: stored.map(c => c.name), count: stored.length }) });
        return;
      }
      if (path.endsWith('/credentials') && method === 'POST') {
        const body = route.request().postDataJSON() as { name: string };
        stored.push({ name: body.name });
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ name: body.name, fields: ['username', 'password'], stored: true }) });
        return;
      }
      if (path.endsWith('/notifications')) {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ notifications: [], unread_count: 0 }) });
        return;
      }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({}) });
    });

    await page.goto('/dashboard/credentials');
    // Wait for React hydration: the submit handler only exists after it.
    await page.waitForLoadState('networkidle');
    await expect(page.getByRole('button', { name: /save/i })).toBeEnabled();
    await page.getByPlaceholder('mysite-login, github, smtp').fill('smoke-cred');
    const passwordInput = page.locator('input[type="password"]').last();
    await passwordInput.fill('S3cret!Value');
    await page.getByRole('button', { name: /save/i }).click();

    await expect(page.getByText(/Stored “smoke-cred”/)).toBeVisible();
    await expect(page.getByText('smoke-cred').first()).toBeVisible({ timeout: 5000 });
    const body = await page.locator('body').innerText();
    // Vault contract (audit): the raw value never appears in the DOM after save
    expect(body).not.toContain('S3cret!Value');
  });
});
