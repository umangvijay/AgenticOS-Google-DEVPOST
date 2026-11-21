import { test, expect, type Page } from '@playwright/test';

async function fixture(page: Page) {
  const user = { id: 'ui-test', name: 'Local tester', email: 'test@example.invalid', role: 'user' };
  await page.addInitScript(u => localStorage.setItem('agentos_auth', JSON.stringify({ accessToken: 'test', refreshToken: 'test', user: u })), user);
  await page.route('**/*', route => new URL(route.request().url()).origin === `http://localhost:${Number(process.env.E2E_PORT || 3000)}` ? route.continue() : route.abort());
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    const body = path === '/auth/me' ? user : path === '/workflows' ? { workflows: [] } :
      path.includes('/notifications') ? { notifications: [], unread_count: 0 } :
      path === '/settings' ? { settings: { default_model: 'gemini-3.8-flash' }, available_models: ['gemini-3.8-flash'] } : {};
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  });
}

test('catalog failure offers retry without claiming an empty catalog', async ({ page }) => {
  await fixture(page);
  let failed = true;
  await page.route('**/api/v1/integrations/catalog', route => route.fulfill({ status: failed ? 500 : 200, contentType: 'application/json', body: JSON.stringify(failed ? { detail: 'Internal server error' } : { integrations: [], count: 0 }) }));
  await page.goto('/dashboard/catalog');
  await expect(page.getByRole('heading', { name: 'Couldn’t load the catalog' })).toBeVisible();
  await expect(page.getByText('The catalog is empty')).toHaveCount(0);
  failed = false;
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.getByText('The catalog is empty')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Build an integration' })).toBeVisible();
  await expect(page.getByText('Internal server error')).toHaveCount(0);
});

test('long settings page stays sharp even with reveal observation unavailable, and failed loads recover', async ({ page }) => {
  await fixture(page);
  // A stalled observer must never hide application content.
  await page.addInitScript(() => { window.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof IntersectionObserver; });
  await page.setViewportSize({ width: 1280, height: 600 });
  let failed = true;
  await page.route('**/api/v1/settings', route => route.fulfill({ status: failed ? 500 : 200, contentType: 'application/json', body: JSON.stringify(failed ? { detail: 'Unavailable' } : { settings: { default_model: 'gemini-3.8-flash' }, available_models: ['gemini-3.8-flash'] }) }));
  await page.goto('/dashboard/settings');
  await expect(page.getByRole('heading', { name: 'Couldn’t load settings' })).toBeVisible();
  failed = false;
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.getByRole('heading', { name: 'Account Profile' })).toBeVisible();
  const content = page.locator('.dashboard-shell [data-scroll-reveal]').first();
  await expect(content).toHaveCSS('filter', 'none');
  await expect(content).toHaveCSS('opacity', '1');
  await page.screenshot({ path: '/tmp/sandtable-settings-fixed.png' });
});

test('integration stays readable and explains blocked tools without pretending enabled means verified', async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route('**/api/v1/integrations/demo', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ mcp_id: 'demo', name: 'Website tools', is_enabled: true, tools: [], operation_eligibility: [{ tool_name: 'open_page', allowed: false, provenance: 'unknown', reason_codes: ['unknown_origin'] }] }) }));
  await page.route('**/api/v1/integrations/demo/revisions', route => route.fulfill({ contentType: 'application/json', body: '{"revisions":[]}' }));
  await page.route('**/api/v1/integrations/demo/test', route => route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"Probe unavailable"}' }));
  await page.goto('/dashboard/integrations/demo');
  await expect(page.getByRole('heading', { name: 'Website tools' })).toBeVisible();
  await expect(page.getByText('Some tools aren’t ready to use')).toBeVisible();
  await expect(page.locator('.dashboard-shell [data-scroll-reveal]')).toHaveCSS('filter', 'none');
  await page.getByRole('button', { name: 'Run Test', exact: true }).click();
  await expect(page.locator('.dashboard-shell').getByRole('alert')).toContainText('Probe unavailable');
  await expect(page.getByRole('heading', { name: 'Website tools' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: '/tmp/sandtable-integration-fixed.png' });
});

test('new chat starts simply and suggestions remain editable before sending', async ({ page }) => {
  await fixture(page);
  let submitted = false;
  page.on('request', request => { if (request.method() === 'POST' && request.url().endsWith('/workflows')) submitted = true; });
  await page.goto('/dashboard');
  await expect(page.getByRole('heading', { name: 'Where should we begin?' })).toBeVisible();
  await expect(page.locator('.mcp-howto')).not.toHaveAttribute('open');
  await page.getByRole('button', { name: 'Summarize a website' }).click();
  await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toHaveValue(/https:\/\/example.com/);
  expect(submitted).toBe(false);
  await page.screenshot({ path: '/tmp/sandtable-new-chat.png' });
});

test('Studio reconnects to the same job and shows the failure beside Generate', async ({ page }) => {
  await fixture(page);
  let posts = 0;
  let polls = 0;
  await page.route('**/api/v1/capabilities/generate', route => {
    posts++;
    return route.fulfill({ json: { job_id: 'studio-job', status: 'queued' } });
  });
  await page.route('**/api/v1/capabilities/generate/studio-job/events', route => route.abort());
  await page.route('**/api/v1/capabilities/generate/studio-job', route => {
    polls++;
    if (polls === 2) return route.fulfill({ status: 503, json: { detail: 'Try again' } });
    return route.fulfill({ json: {
      job_id: 'studio-job', stage: 'writing_files', completed_files: 0, total_files: 41,
      status: polls === 1 ? 'running' : 'failed', error: 'Model did not finish a required file: styles.css',
    } });
  });
  await page.goto('/dashboard/studio');
  const card = page.locator('section').filter({ has: page.getByRole('heading', { name: 'Generate a website or app' }) });
  await card.getByRole('textbox').fill('Make a clothing site');
  await card.getByRole('button', { name: 'Generate', exact: true }).click();
  await expect(card.getByRole('status')).toContainText('0 / 41');
  await expect(card.getByRole('button', { name: 'Generating…' })).toBeDisabled();
  await expect(card.getByRole('alert')).toContainText('styles.css', { timeout: 12000 });
  await expect(card.getByRole('status')).toContainText('Generation failed');
  await expect(card.getByRole('button', { name: 'Generate', exact: true })).toBeEnabled();
  expect(posts).toBe(1);
});

test('dashboard keeps a chosen theme and the recent-chat disclosure on reload', async ({ page }) => {
  await fixture(page);
  await page.goto('/');
  await page.evaluate(() => localStorage.setItem('agentos_theme', 'dark'));
  await page.goto('/dashboard/workflows');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  const recent = page.getByRole('button', { name: 'Recent chats' });
  await recent.click();
  await expect(recent).toHaveAttribute('aria-expanded', 'false');
  await page.getByRole('button', { name: 'Switch to day theme' }).click();
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await expect(recent).toHaveAttribute('aria-expanded', 'false');
  await recent.click();
  await expect(recent).toHaveAttribute('aria-expanded', 'true');
});

test('a disabled stamp explains a failed 404 and only enables after a passing retest', async ({ page }) => {
  await fixture(page);
  let ready = false;
  await page.route('**/api/v1/integrations/demo', route => route.fulfill({ json: {
    mcp_id: 'demo', name: 'Demo stamp', transport: 'internal', is_enabled: false,
    trust_tier: ready ? 'verified' : 'pending_review', validated_tool_count: ready ? 1 : 0, tools: [],
  } }));
  await page.route('**/api/v1/integrations/demo/revisions', route => route.fulfill({ json: { revisions: [] } }));
  await page.route('**/api/v1/integrations/demo/test', route => route.fulfill({ json: ready ? { status: 'healthy' } : { status: 'unhealthy', probe: { http_status: 404 } } }));
  await page.goto('/dashboard/integrations/demo');
  const enable = page.getByRole('button', { name: 'Enable', exact: true });
  await expect(enable).toBeDisabled();
  await page.getByRole('button', { name: 'Run Test' }).click();
  await expect(page.getByText(/provider returned 404/)).toBeVisible();
  ready = true;
  await page.getByRole('button', { name: 'Run Test' }).click();
  await expect(enable).toBeEnabled();
});
