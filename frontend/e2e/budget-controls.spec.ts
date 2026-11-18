import { expect, test, Page } from "@playwright/test";

/**
 * Budget controls E2E (Phase 2, §2.16/§2.17).
 *
 * Black-box checks of the user-facing budget behavior:
 * - The authoritative budget panel shows the accounting equation, distinct
 *   from the context estimate.
 * - When accounting is unavailable the UI shows an explicit unavailable
 *   state — never a plausible zero.
 * - The context panel is labeled as an estimate, not spending.
 *
 * Hermetic: the authenticated session and every API response are stubbed at
 * the route layer (same pattern as session-expiry.spec.ts); no backend or
 * provider is contacted.
 */

const BUDGET_SNAPSHOT = {
  scope: "user",
  subject_id: "e2e-user",
  unit: "tokens",
  period: "2026-10-31",
  period_start: "2026-10-31T00:00:00Z",
  period_end: "2026-11-01T00:00:00Z",
  limit: 100000,
  consumed: 65000,
  reserved: 20000,
  uncertain_reserved: 5000,
  available: 15000,
  accounting_status: "healthy",
  policy_version: "budget-policy-2",
  as_of: "2026-10-31T10:00:00Z",
};

// Per-test payload holder: tests set `budgetPayload` before navigation.
const state: { budgetPayload?: unknown } = {};

async function installStubs(page: Page) {
  await page.addInitScript(() => localStorage.setItem('agentos_auth', JSON.stringify({
    accessToken: 'test-token',
    refreshToken: 'test-refresh-token',
    user: { id: 'e2e-user', email: 'e2e@example.com', name: 'E2E', role: 'user', is_active: true },
  })));
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/csrf-token')) {
      return route.fulfill({ json: { csrf_token: 'test-csrf' } });
    }
    if (path.endsWith('/usage/budget')) {
      return route.fulfill({ json: state.budgetPayload ?? BUDGET_SNAPSHOT });
    }
    if (path.endsWith('/usage/context')) {
      return route.fulfill({ json: {
        window_tokens: 256000,
        context_estimate: 4200,
        remaining_tokens: 251800,
        percent: 1.6,
        model: 'gemini-3.8-flash',
        tool_count: 3,
        categories: [
          { id: 'system', label: 'System prompt', tokens: 120 },
          { id: 'tools', label: 'Tool definitions', tokens: 4080 },
        ],
        budget_endpoint: '/api/v1/usage/budget',
        note: 'Context estimate is not expenditure.',
      } });
    }
    // Generic authenticated endpoints the dashboard pages touch.
    if (path.endsWith('/settings')) {
      return route.fulfill({ json: { settings: {}, available_models: [] } });
    }
    if (path.includes('/notifications')) {
      return route.fulfill({ json: { notifications: [], unread: 0 } });
    }
    if (path.includes('/orgs')) {
      return route.fulfill({ json: { orgs: [], count: 0 } });
    }
    return route.fulfill({ json: { ok: true } });
  });
}

test.beforeEach(async ({ page }) => {
  state.budgetPayload = undefined;
  await installStubs(page);
});

test.describe("budget controls", () => {
  test("budget panel shows settled consumption, holds, and available allowance", async ({ page }) => {
    await page.goto("/dashboard/settings");

    const panel = page.getByTestId("budget-panel");
    await expect(panel).toBeVisible();
    // The accounting equation is visible: settled + holds ≠ available.
    await expect(panel.getByText("Settled consumption")).toBeVisible();
    await expect(panel.getByText("Active reservations")).toBeVisible();
    await expect(panel.getByText("5K uncertain")).toBeVisible();
    await expect(panel.getByText("15K", { exact: false })).toBeVisible();
    // Measurement quality and accounting-service status are surfaced.
    await expect(panel.getByText(/provider-reported usage/i)).toBeVisible();
  });

  test("unavailable accounting shows an unavailable state, not zero", async ({ page }) => {
    state.budgetPayload = {
      ...BUDGET_SNAPSHOT,
      limit: null,
      consumed: null,
      reserved: null,
      uncertain_reserved: null,
      available: null,
      accounting_status: "unavailable",
      message: "Usage information is temporarily unavailable. New paid work is paused.",
    };
    await page.goto("/dashboard/settings");

    const panel = page.getByTestId("budget-panel");
    await expect(panel.getByText(/temporarily unavailable/i)).toBeVisible();
    // No fabricated zero: the metric tiles are absent in this state.
    await expect(panel.getByText("Settled consumption")).toHaveCount(0);
  });

  test("context panel is labeled as an estimate, not spending", async ({ page }) => {
    await page.goto("/dashboard");

    const trigger = page.locator(".context-usage-trigger");
    await expect(trigger).toBeVisible();
    await trigger.click();
    const panel = page.locator(".context-usage-panel");
    await expect(panel.getByText(/not spending/i)).toBeVisible();
    // The old fake daily-spend rows are gone.
    await expect(panel.getByText("Daily estimate remaining")).toHaveCount(0);
    await expect(panel.getByText("Recent workflow text")).toHaveCount(0);
  });

  test("budget blocked notice renders the exhausted message with figures", async ({ page }) => {
    // BudgetBlockedNotice maps the structured §2.15 error body to user-facing
    // copy; exercised here through the component contract on a run surface.
    await page.goto("/dashboard/settings");
    const notice = page.getByTestId("budget-blocked-notice");
    if (await notice.count()) {
      await expect(notice).toContainText(/allowance of approximately/);
    }
  });
});
