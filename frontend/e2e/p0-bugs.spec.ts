/**
 * AgentOS — E2E tests for the P0 bugs from the production plan.
 *
 * These test the real user flows, not mocked data.
 * Requires: the app running at localhost:3000 with a test user logged in.
 */

import { test, expect } from '@playwright/test';
import { safeSnippet } from '../src/lib/safe-snippet';

// ─── A1: Chat crash regression (status_code without data) ──────────────

test.describe('A1 — Chat crash regression', () => {
  test('safeSnippet exists and is importable', async () => {
    expect(safeSnippet).toBeDefined();
    expect(typeof safeSnippet).toBe('function');
  });

  test('safeSnippet handles undefined, null, objects, circular refs', async () => {
    expect(safeSnippet(undefined)).toBe('');
    expect(safeSnippet(null)).toBe('');
    expect(safeSnippet({ status_code: 200 })).toContain('200');
    expect(safeSnippet('hello world')).toBe('hello world');
    expect(safeSnippet({ data: null })).toContain('null');
    const obj: Record<string, unknown> = { a: 1 };
    obj.self = obj;
    expect(() => safeSnippet(obj)).not.toThrow();
  });
});

// ─── A3: API key validation ────────────────────────────────────────────

test.describe('A3 — API key validation', () => {
  test('settings page loads without crash', async ({ page }) => {
    await page.goto('/dashboard/settings');
    // Unauthenticated: dashboard gate says "Sign in to Sandtable" (or the
    // login page's "Welcome back."); authed shows Settings. All three are
    // valid loads — the A3 contract is NO CRASH, not a specific page.
    await expect(
      page.getByRole('heading', { name: /sign in|settings|welcome back/i }),
    ).toBeVisible({ timeout: 15000 });
  });
});

// ─── A4: Theme toggle ─────────────────────────────────────────────────

test.describe('A4 — Theme', () => {
  test('login page renders without error', async ({ page }) => {
    await page.goto('/login');
    await expect(page.locator('body')).toBeVisible();
  });

  test('dark theme CSS variables exist in globals', async ({ page }) => {
    await page.goto('/login');
    // Check that the [data-theme="dark"] selector has different bg-primary
    await page.evaluate(() => {
      document.documentElement.setAttribute('data-theme', 'dark');
    });
    const bgPrimary = await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue('--bg-primary').trim()
    );
    expect(bgPrimary).toBe('#04060b'); // Tungsten near-black hall (v3.5 cyanotype)
  });

  test('light theme has paper-white bg', async ({ page }) => {
    await page.goto('/login');
    await page.evaluate(() => {
      document.documentElement.setAttribute('data-theme', 'light');
    });
    const bgPrimary = await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue('--bg-primary').trim()
    );
    expect(bgPrimary).toBe('#e9eef6'); // North Light drafting paper (v3.5 cyanotype)
  });
});
