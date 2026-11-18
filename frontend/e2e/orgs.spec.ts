import { test, expect } from '@playwright/test';

/**
 * E2E for the Organizations UI (RBAC foundation): create, membership by
 * email, role ladder, owner protection, and run oversight.
 */

const authInit = () => localStorage.setItem('agentos_auth', JSON.stringify({
  accessToken: 'test-only-token', refreshToken: 'r',
  user: { id: 'owner-1', email: 'owner@example.com', name: 'Owner', role: 'user', is_active: true },
}));

const org = { org_id: 'org-1', name: 'Acme Corp', owner_id: 'owner-1', created_at: '2026-10-18T00:00:00Z', role: 'owner' };
const members = [
  { user_id: 'owner-1', role: 'owner', email: 'owner@example.com', name: 'Owner' },
  { user_id: 'admin-1', role: 'admin', email: 'admin@example.com', name: 'Admin' },
  { user_id: 'mem-1', role: 'member', email: 'member@example.com', name: 'Member' },
];

test.describe('Organizations', () => {
  test('creates an org, adds a member by email, enforces owner protection, shows oversight', async ({ page }) => {
    const state = { members: [...members] };
    let invitationCount = 0;
    await page.addInitScript(authInit);
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      const method = route.request().method();
      if (path.endsWith('/orgs') && method === 'GET') {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ orgs: state.members.length ? [org] : [], count: state.members.length ? 1 : 0 }) });
        return;
      }
      if (path.endsWith('/orgs') && method === 'POST') {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ...org, role: 'owner' }) });
        return;
      }
      if (path.endsWith('/org-1') && method === 'GET') {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ...org, members: state.members }) });
        return;
      }
      if (path.endsWith('/org-1/invitations') && method === 'POST') {
        const body = route.request().postDataJSON() as { email: string; role: string };
        // Invitations are anti-enumeration: unknown emails still receive a
        // pending invite; membership is created on ACCEPTANCE, not here.
        invitationCount += 1;
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ invitation_id: `inv-${invitationCount}`, org_id: 'org-1', email: body.email, role: body.role, expires_at: '2026-11-06T00:00:00Z', token: `one-time-token-${invitationCount}` }) });
        return;
      }
      if (path === '/api/v1/orgs/org-1/members/owner-1' && method === 'DELETE') {
        await route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'The owner cannot be removed' }) });
        return;
      }
      if (path.includes('/role') && method === 'POST') {
        const body = route.request().postDataJSON() as { role: string };
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ org_id: 'org-1', user_id: 'mem-1', role: body.role }) });
        return;
      }
      if (path.endsWith('/org-1/runs')) {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({
          org_id: 'org-1', count: 1,
          runs: [{ run_id: 'run-9', goal: 'check the health of https://example.com', status: 'COMPLETED', created_at: '2026-10-18T00:00:00Z', member_email: 'owner@example.com', user_id: 'owner-1', tasks: [{ task_id: 't', title: 'core.health', agent: 'core.health', status: 'COMPLETED', error: null }] }],
        }) });
        return;
      }
      if (path.endsWith('/notifications')) {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ notifications: [], unread_count: 0 }) });
        return;
      }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({}) });
    });

    await page.goto('/dashboard/orgs');
    await page.getByPlaceholder('Organization name').fill('Acme Corp');
    await page.getByRole('button', { name: 'Create' }).click();
    await expect(page.getByText(/Organization "Acme Corp" created/)).toBeVisible();
    await expect(page.getByText('owner@example.com').first()).toBeVisible();

    // Invite a member by email — membership activates on ACCEPTANCE
    await page.getByPlaceholder('teammate@company.com').fill('new@company.com');
    await page.getByRole('button', { name: 'Invite' }).click();
    await expect(page.getByText(/Invitation token — shown once/)).toBeVisible();
    await expect(page.getByText('one-time-token-1')).toBeVisible();

    // Anti-enumeration: unknown emails receive an invitation too (no user
    // existence leak), with its own one-time token.
    await page.getByPlaceholder('teammate@company.com').fill('ghost@nowhere.com');
    await page.getByRole('button', { name: 'Invite' }).click();
    await expect(page.getByText('one-time-token-2')).toBeVisible();

    // Owner protection: remove button is absent for the owner row
    const ownerRow = page.locator('tr', { hasText: 'owner@example.com' });
    await expect(ownerRow.getByRole('button', { name: 'Remove' })).toHaveCount(0);

    // Run oversight with scoped member attribution
    await page.getByRole('button', { name: 'Run oversight' }).click();
    await expect(page.getByText(/Member runs \(read-only oversight\)/)).toBeVisible();
    await expect(page.getByText('check the health of https://example.com')).toBeVisible();
    await expect(page.getByText('owner@example.com ·')).toBeVisible();
  });

  test('empty state guides the user', async ({ page }) => {
    await page.addInitScript(authInit);
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith('/orgs') && route.request().method() === 'GET') {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ orgs: [], count: 0 }) });
        return;
      }
      if (path.endsWith('/notifications')) {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ notifications: [], unread_count: 0 }) });
        return;
      }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({}) });
    });
    await page.goto('/dashboard/orgs');
    await expect(page.getByText('No organizations yet')).toBeVisible();
  });
});
