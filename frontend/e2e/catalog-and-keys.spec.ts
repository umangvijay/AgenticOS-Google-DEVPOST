import { test, expect } from '@playwright/test';
import type { ApiKeyRow } from '@/lib/api';

/**
 * E2E for the Round-8/9 features: the shared catalog and API-key manager.
 * Backend routes are mocked — these verify the real UI flows and contracts.
 */

const catalogEntry = {
  mcp_id: 'poke1',
  name: 'PokeAPI',
  description: 'List and fetch Pokemon',
  source_type: 'url',
  trust_tier: 'verified',
  tool_count: 2,
  tools: [{ name: 'list_pokemon' }, { name: 'get_pokemon' }],
};

test.describe('Catalog page', () => {
  test('shows published integrations and installs one', async ({ page }) => {
    let installed = false;
    await page.addInitScript(() => localStorage.setItem('agentos_auth', JSON.stringify({
      accessToken: 'test-only-token', refreshToken: 'test-only-refresh',
      user: { id: 'test-user', email: 't@example.invalid', name: 'T', role: 'user', is_active: true },
    })));
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith('/integrations/catalog')) {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ integrations: installed ? [] : [catalogEntry], count: installed ? 0 : 1 }) });
        return;
      }
      if (path.endsWith('/integrations/poke1/install')) {
        installed = true;
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ mcp_id: 'clone1', installed: true, tools: 2, name: 'PokeAPI' }) });
        return;
      }
      if (path.endsWith('/integrations')) {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ integrations: [], count: 0 }) });
        return;
      }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({}) });
    });
    await page.goto('/dashboard/catalog');
    await expect(page.getByRole('heading', { name: 'PokeAPI' })).toBeVisible();
    await expect(page.locator('.badge-success').getByText('verified')).toBeVisible();
    await page.getByRole('button', { name: 'Show tools' }).click();
    await expect(page.getByText('list_pokemon')).toBeVisible();
    await page.getByRole('button', { name: 'Install' }).click();
    await expect(page.getByText(/Installed "PokeAPI" with 2 tools/)).toBeVisible();
  });

  test('empty catalog renders the honest empty state', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('agentos_auth', JSON.stringify({
      accessToken: 'test-only-token', refreshToken: 'test-only-refresh',
      user: { id: 'test-user', email: 't@example.invalid', name: 'T', role: 'user', is_active: true },
    })));
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith('/integrations/catalog')) {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ integrations: [], count: 0 }) });
        return;
      }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({}) });
    });
    await page.goto('/dashboard/catalog');
    await expect(page.getByText('The catalog is empty')).toBeVisible();
  });
});

test.describe('API keys manager', () => {
  test('creates a key, shows it once, lists and revokes', async ({ page }) => {
    const keys: ApiKeyRow[] = [{
      id: 'legacy-inactive', name: 'Old deployment', prefix: 'old-key',
      created_at: '2026-10-01T00:00:00Z', is_active: false, revoked_at: null,
    }];
    let seq = 0;
    let lastCreated: string | null = null;
    await page.addInitScript(() => localStorage.setItem('agentos_auth', JSON.stringify({
      accessToken: 'test-only-token', refreshToken: 'test-only-refresh',
      user: { id: 'test-user', email: 't@example.invalid', name: 'T', role: 'user', is_active: true },
    })));
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith('/auth/api-keys') && route.request().method() === 'POST') {
        seq += 1;
        const id = `key-${seq}`;
        const raw = `vry_raw${seq}`;
        lastCreated = raw;
        keys.push({ id, name: 'ci', prefix: raw.slice(0, 8), created_at: '2026-10-17T00:00:00Z', last_used_at: null, revoked_at: null });
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ id, name: 'ci', key: raw, prefix: raw.slice(0, 8), warning: 'Store this key now' }) });
        return;
      }
      if (path.endsWith('/auth/api-keys') && route.request().method() === 'GET') {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ keys, count: keys.length }) });
        return;
      }
      if (path.includes('/auth/api-keys/') && route.request().method() === 'DELETE') {
        const id = path.split('/').pop()!;
        const row = keys.find(k => k.id === id);
        if (row) row.revoked_at = '2026-10-17T01:00:00Z';
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ id, revoked: true }) });
        return;
      }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({}) });
    });
    await page.goto('/dashboard/settings');
    const legacy = page.getByRole('row', { name: /Old deployment/ });
    await expect(legacy.getByText('revoked')).toBeVisible();
    await expect(legacy.getByRole('button', { name: 'Revoke' })).toHaveCount(0);
    await page.getByPlaceholder('Key name (e.g. ci-pipeline)').fill('ci');
    await page.getByRole('button', { name: 'Create key' }).click();
    // raw key shown once
    await expect(page.getByText('vry_raw1', { exact: true })).toBeVisible();
    await expect(page.getByText(/never shown again/i)).toBeVisible();
    await page.getByRole('button', { name: 'Done' }).click();
    // listed with prefix only, never the raw key
    await expect(page.getByText('vry_raw1…')).toBeVisible();
    const body = await page.locator('body').innerText();
    expect(body).not.toContain('vry_raw1\n');
    // revoke
    page.on('dialog', d => d.accept());
    await page.getByRole('button', { name: 'Revoke' }).click();
    await expect(page.getByRole('row', { name: /vry_raw1/ }).getByText('revoked')).toBeVisible();
    expect(lastCreated).toBeTruthy();
  });
});
