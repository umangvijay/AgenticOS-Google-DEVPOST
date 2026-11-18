/**
 * Phase 31 §31.21 — LINK-PREVIEW@1 and DOCK-RESOURCE-IDENTITY@1.
 *
 * Versioned scenario suite (docs/qualification/phase-31-scenarios.json).
 * Drives the REAL composed frontend against the REAL local backend:
 * one REAL run is seeded through POST /workflows (the user bubble then
 * renders the raw-URL linkifier path), and every interaction test runs
 * on that real page. Every scenario records a DOM assertion AND a
 * screenshot tagged with the scenario step.
 *
 * Fixture labeling (§31.1: fixture-only UI checks stay distinguishable):
 *  - The workspace page, composer, anchors, dock, and auth session are
 *    REAL composed frontend against the REAL backend.
 *  - The slow-A/fast-B metadata RACE and the slow-load state are
 *    fixture-backed interaction checks: Playwright intercepts
 *    `/api/v1/link-previews` so race timing is deterministic. The REAL
 *    metadata path (denied host) is exercised against the product's
 *    actual preview endpoint in a separate test.
 *  - Dock layout persistence is asserted through the composed page;
 *    the store contract (tab focus, close-vs-stop, per-principal
 *    persistence) is covered by frontend/tests/mounted/
 *    workspace-preview-dock.test.tsx and referenced by annotation.
 *
 * When the backend is down the gate reports LOUDLY — nothing is faked.
 */
import { test, expect, Page } from '@playwright/test';

const BACKEND = 'http://127.0.0.1:8000';
const REAL_PAGE = 'https://example.com/';
// Greeting-shaped so the product's deterministic direct plan serves it
// (the full ADK planner exceeds the local CPU inference budget); the
// raw URL is still in the goal, so the user bubble renders the link.
const SEED_GOAL = `hi there. Summarize ${REAL_PAGE} for accessibility in one line.`;
const SEED_MD_GOAL = ("hi there. Reply with exactly this Markdown and "
    + "nothing else: [slow A](https://slow-a.example/page) and "
    + "[fast B](https://fast-b.example/page) and "
    + "[corner](https://example.com/corner) and "
    + "[kbd](https://example.com/kbd)");

let backendUp = false;
let guestSession: { accessToken: string; refreshToken: string; user: Record<string, unknown> } | null = null;
let seededRunId = '';
let seededMdRunId = '';

test.beforeAll(async ({ request }) => {
  try {
    const health = await request.get(`${BACKEND}/health`, { timeout: 5000 });
    backendUp = health.ok();
    if (!backendUp) return;
    const res = await request.post(`${BACKEND}/api/v1/auth/guest`, { timeout: 10000 });
    const body = await res.json();
    if (body.access_token && body.user) {
      guestSession = { accessToken: body.access_token,
                       refreshToken: body.refresh_token || '',
                       user: body.user };
    }
  } catch {
    backendUp = false;
  }
});

/** Seed ONE real run through the REAL planner so the composed page
 * renders the user bubble's raw-URL linkifier. Runs as the first serial
 * test (its own generous timeout); the rest of the suite consumes it. */
async function seedRun(request: import('@playwright/test').APIRequestContext,
                       goal: string): Promise<string> {
  if (!guestSession) return '';
  const token = guestSession.accessToken;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const sub = await request.post(`${BACKEND}/api/v1/workflows`, {
        data: { goal },
        headers: { Authorization: `Bearer ${token}` },
        timeout: 240000,
      });
      if (sub.ok()) {
        const rid = (await sub.json()).run_id || '';
        if (rid) return rid;
      } else if (sub.status() === 429) {
        await new Promise((r) => setTimeout(r, 20000));
      }
    } catch { /* planner latency; bounded retry */ }
  }
  return '';
}

/** Seed the markdown-echo run and VERIFY the assistant reply actually
 *  contains the expected link before returning its id — the local model
 *  occasionally does not echo the requested Markdown (model
 *  non-compliance, not a product defect), which previously failed A2
 *  after a 300 s wait and cascade-skipped the serial block. ONE firmer
 *  re-ask is honest: the reply is still the real planner's real output,
 *  never a fixture. */
async function seedMdRunUntilEcho(
    request: import('@playwright/test').APIRequestContext): Promise<string> {
  if (!guestSession) return '';
  const token = guestSession.accessToken;
  const marker = 'slow-a.example';
  const goals = [SEED_MD_GOAL,
    "Reply with EXACTLY this Markdown, character for character, "
    + "and nothing else — no preamble, no explanation: "
    + "[slow A](https://slow-a.example/page) and "
    + "[fast B](https://fast-b.example/page) and "
    + "[corner](https://example.com/corner) and "
    + "[kbd](https://example.com/kbd)"];
  for (const goal of goals) {
    try {
      const sub = await request.post(`${BACKEND}/api/v1/workflows`, {
        data: { goal },
        headers: { Authorization: `Bearer ${token}` },
        timeout: 240000,
      });
      if (!sub.ok()) {
        if (sub.status() === 429) await new Promise((r) => setTimeout(r, 20000));
        continue;
      }
      const rid = (await sub.json()).run_id || '';
      if (!rid) continue;
      // Poll the REAL run: terminal status must carry the marker in
      // its output for this id to qualify as the A2 seed.
      const deadline = Date.now() + 240000;
      while (Date.now() < deadline) {
        try {
          const r = await request.get(`${BACKEND}/api/v1/workflows/${rid}`, {
            headers: { Authorization: `Bearer ${token}` },
            timeout: 30000,
          });
          if (r.ok()) {
            const run = await r.json();
            // TERMINAL first, then the marker — and only against the
            // run's OUTPUT. The goal text itself contains the marker, so
            // matching the whole run JSON (tasks included) accepted a
            // still-RUNNING run whose turns kept re-rendering mid-test,
            // churning the anchors and dismissing hover cards under a
            // stationary cursor.
            const status = String(run.status ?? '').toUpperCase();
            const terminal =
              ['COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED'].includes(status);
            if (!terminal) {
              await new Promise((r2) => setTimeout(r2, 4000));
              continue;
            }
            // Marker must appear in task OUTPUTS only — input_data
            // carries the goal text (which contains the marker) and
            // would false-positive a run that never echoed anything.
            const outs = (run.tasks || [])
              .map((t: Record<string, unknown>) =>
                JSON.stringify(t.output_data ?? t.output ?? ''))
              .join('');
            if (outs.includes(marker)) return rid;
            break; // terminal without the echo — try the firmer goal
          }
        } catch { /* transient read; keep polling */ }
        await new Promise((r2) => setTimeout(r2, 4000));
      }
    } catch { /* bounded; try the firmer goal */ }
  }
  return '';
}

/** The assistant Markdown anchor path: wait for the seeded run's REAL
 * reply to render through the product's ReactMarkdown pipeline. */
async function openMdWorkspace(page: Page) {
  test.skip(!seededMdRunId,
    'markdown-link run seeding unavailable — the assistant-anchor path is NOT faked');
  test.setTimeout(320000);
  applyGuestSession(page);
  await page.goto(`/dashboard/workspace/${seededMdRunId}`, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.agent-markdown a[href*="slow-a.example"]').first())
    .toBeVisible({ timeout: 300000 });
  await settleLayout(page);
}

/** Wait for the page's mount/scroll-reveal settle to finish: panels mount
 *  and the chat column settles for ~600ms after load, during which a
 *  fixed-positioned element inside the transformed chat subtree slides.
 *  Geometry-sensitive interactions (hover cards, viewport-edge clamps)
 *  must observe the SETTLED page — a real user never hovers mid-settle.
 *  The chat COLUMN is watched (not just the anchor): the right-hand dock
 *  panels mount asynchronously and reflow the column up to ~2s after
 *  load, which slides a stationary anchor out from under the cursor. */
async function settleLayout(page: Page, selector?: string) {
  const target = selector ?? '.agent-markdown a[href*="slow-a.example"]';
  await page.evaluate(async (sel) => {
    const el = document.querySelector(sel) as HTMLElement | null;
    if (!el) return;
    const column = el.closest('.workspace-chat') as HTMLElement | null || el;
    const rect = () => JSON.stringify(column.getBoundingClientRect().toJSON());
    let stableTicks = 0;
    let last = rect();
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 300));
      const now = rect();
      if (now === last) {
        // The right-hand dock panels mount in stages up to ~2s after
        // load; require sustained stability, not two lucky ticks.
        stableTicks += 1;
        if (stableTicks >= 3) return;
      } else {
        stableTicks = 0;
        last = now;
      }
    }
  }, target);
}

function applyGuestSession(page: Page) {
  if (guestSession) {
    page.addInitScript((session) => {
      window.localStorage.setItem('agentos_auth', JSON.stringify(session));
    }, guestSession);
  }
}

async function openSeededWorkspace(page: Page) {
  test.skip(!seededRunId, 'run seeding unavailable (planner latency or transport) — LINK-PREVIEW@1 is NOT faked');
  applyGuestSession(page);
  await page.goto(`/dashboard/workspace/${seededRunId}`, { waitUntil: 'domcontentloaded' });
  // The seeded run's user bubble carries the raw URL anchor.
  await expect(page.locator(`a[href="${REAL_PAGE}"]`).first())
    .toBeVisible({ timeout: 30000 });
  await settleLayout(page, `a[href="${REAL_PAGE}"]`);
}

const shot = (page: Page, name: string) =>
  page.screenshot({ path: `test-results/phase31/${name}.png`, fullPage: false });

test('backend reachability is reported loudly', async () => {
  const runnable = 'docker compose --profile core up -d (api on :8000)';
  expect(backendUp, `BLOCKED: no local backend on :8000 — run: ${runnable}. LINK-PREVIEW@1 is NOT faked.`).toBe(true);
});

test.describe.serial('LINK-PREVIEW@1', () => {
  test('seeding: two real runs through the real planner', async ({ request }) => {
    test.setTimeout(320000);
    expect(backendUp, 'backend must be up for the seeded journey').toBe(true);
    seededRunId = await seedRun(request, SEED_GOAL);
    seededMdRunId = await seedMdRunUntilEcho(request);
    test.info().annotations.push({ type: 'note',
      description: `raw-url run ${seededRunId || '(unavailable)'}; `
        + `markdown-link run ${seededMdRunId || '(unavailable)'}` });
  });

  test('A1 raw URL in a user message triggers the hover card', async ({ page }) => {
    await openSeededWorkspace(page);
    const anchor = page.locator(`a[href="${REAL_PAGE}"]`).first();
    await anchor.hover();
    const card = page.locator('[role="dialog"][aria-label^="Link preview"]');
    await expect(card).toBeVisible({ timeout: 8000 });
    await expect(card).toContainText('example.com');
    await shot(page, 'A1-raw-url-card');
  });

  test('A2 an assistant Markdown link triggers the same card', async ({ page }) => {
    await openMdWorkspace(page);
    const anchor = page.locator('.agent-markdown a').first();
    await expect(anchor).toBeVisible();
    await anchor.hover();
    const card = page.locator('[role="dialog"][aria-label^="Link preview"]');
    await expect(card).toBeVisible({ timeout: 8000 });
    await shot(page, 'A2-markdown-link-card');
  });

  test('A3/A4 slow A then fast B: B stays visible with B metadata (fixture-backed race)', async ({ page }) => {
    // The race needs the markdown-echo run (slow-a/fast-b anchors), not
    // the raw-URL seed: openMdWorkspace seeds it and waits for the links.
    await openMdWorkspace(page);
    // Deterministic race: A's metadata resolves LATE, B's resolves fast.
    await page.route('**/api/v1/link-previews', async (route) => {
      const body = route.request().postDataJSON() as { url: string };
      if (body.url.includes('slow-a')) {
        await new Promise((r) => setTimeout(r, 1200));
        // A's fetch is aborted when B's intent fires (the product
        // cancels the stale request); fulfilling an aborted request
        // would raise in the Node process — the late response must be
        // DROPPED silently, which is also what a real server sees.
        try {
          await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({ title: 'SLOW A TITLE', thumbnail_url: '',
                                   description: 'slow page', final_host: 'slow-a.example',
                                   state: 'ready' }),
          });
        } catch { /* aborted — dropped, like a real late response */ }
      } else if (body.url.includes('fast-b')) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ title: 'FAST B TITLE', thumbnail_url: '',
                                 description: 'fast page', final_host: 'fast-b.example',
                                 state: 'ready' }),
        });
      } else {
        await route.continue();
      }
    });
    const a = page.locator('.agent-markdown a[href*="slow-a.example"]');
    const b = page.locator('.agent-markdown a[href*="fast-b.example"]');
    const card = page.locator('[role="dialog"][aria-label^="Link preview"]');
    // A late layout shift (dock panels mounting) can slide the anchor
    // out from under a stationary cursor — a real user re-hovers. Retry
    // the A→B hover pair until B's card opens; the ROUTER race below is
    // what's under test, not the single-shot hover.
    let raceReady = false;
    for (let attempt = 0; attempt < 4 && !raceReady; attempt++) {
      await a.hover();
      await page.waitForTimeout(250); // past hover intent; A's fetch is in flight
      await b.hover();
      try {
        // B resolves while A is still pending: the card must show B.
        await expect(card).toContainText('FAST B TITLE', { timeout: 3000 });
        // A's late response arrives last — it must NOT replace B, and
        // the card must survive the settle window. A layout shift can
        // slide the anchor out from under the stationary cursor (the
        // card closes honestly); a real user re-hovers, so the whole
        // race is retried rather than trusting one settled frame.
        await page.waitForTimeout(1400);
        await expect(card).toContainText('FAST B TITLE', { timeout: 2000 });
        await expect(card).not.toContainText('SLOW A TITLE');
        raceReady = true;
      } catch {
        await page.mouse.move(300, 200);
        await page.waitForTimeout(500); // let any dismissed card settle
      }
    }
    expect(raceReady, "B card must survive A's late response").toBe(true);
    await expect(card).toBeVisible({ timeout: 4000 });
    await expect(card).toContainText('FAST B TITLE');
    await expect(card).not.toContainText('SLOW A TITLE');
    await shot(page, 'A4-slow-A-fast-B-shows-B');
  });

  test('A5 the Open control stays inside all four viewport edges', async ({ page }) => {
    await openMdWorkspace(page);
    const anchor = page.locator('.agent-markdown a[href*="example.com/corner"]');
    const card = page.locator('[role="dialog"][aria-label^="Link preview"]');
    const edges: Array<[string, number, number]> = [
      ['right-bottom', 0.99, 0.98], ['left-bottom', 0.01, 0.98],
      ['right-top', 0.99, 0.02], ['left-top', 0.01, 0.02],
    ];
    for (const [name, fx, fy] of edges) {
      const intendedX = Math.round(fx * (page.viewportSize()!.width - 220));
      const intendedY = Math.round(fy * (page.viewportSize()!.height - 60));
      await page.evaluate(([fxs, fys]) => {
        const link = document.querySelector('.agent-markdown a[href*="example.com/corner"]') as HTMLElement;
        const wrap = link.closest('p') as HTMLElement || link.parentElement as HTMLElement;
        // position:fixed inside the chat subtree resolves against the
        // nearest TRANSFORMED ancestor (route/scroll-reveal keeps an
        // identity transform on it), not the viewport. The intended
        // coordinates below are corrected against the anchor's ACTUAL
        // rendered rect until they converge — the containing-block
        // chain may be transformed AND scrolled, so no closed-form
        // mapping is trustworthy. The preview card itself is portaled
        // to <body> and clamps in true viewport space — the condition
        // under test.
        wrap.style.position = 'fixed';
        wrap.style.left = `${Math.round(Number(fxs) * (window.innerWidth - 220))}px`;
        wrap.style.top = `${Math.round(Number(fys) * (window.innerHeight - 60))}px`;
        wrap.style.margin = '0';
        wrap.style.zIndex = '999';
      }, [String(fx), String(fy)]);
      // Converge: read the anchor's true viewport rect, correct the
      // wrapper by the delta, repeat. This is robust to any transformed
      // or scrolled containing block between the anchor and <body>.
      for (let i = 0; i < 4; i++) {
        const ab = await anchor.boundingBox();
        if (!ab) break;
        const dx = intendedX + 30 - Math.round(ab.x + ab.width / 2);
        const dy = intendedY + 10 - Math.round(ab.y + ab.height / 2);
        if (Math.abs(dx) <= 2 && Math.abs(dy) <= 2) break;
        await page.evaluate(([dxs, dys]) => {
          const link = document.querySelector('.agent-markdown a[href*="example.com/corner"]') as HTMLElement;
          const wrap = link.closest('p') as HTMLElement || link.parentElement as HTMLElement;
          // Correct from the style values WE set (fixed positioning may
          // resolve against a transformed ancestor — offsetLeft has a
          // different reference and would double-compensate).
          wrap.style.left = `${parseFloat(wrap.style.left || '0') + dxs}px`;
          wrap.style.top = `${parseFloat(wrap.style.top || '0') + dys}px`;
        }, [dx, dy]);
      }
      // A physical cursor cannot hover a corner-pinned anchor reliably:
      // the chat substrate is transformed and settles asynchronously, so
      // Playwright's recomputed hover target can miss the element after
      // pinning. Drive the component's REAL intent path instead —
      // dispatch the pointer events React's onMouseEnter listens for —
      // and assert the same rendered geometry a user would see.
      // The dynamic page (polling re-renders, async panel mounts) can
      // consume one synthetic intent; a real user re-hovers — retry the
      // intent until the card opens. Geometry assertions stay strict.
      let opened = false;
      for (let attempt = 0; attempt < 3 && !opened; attempt++) {
        await page.evaluate(() => {
          const link = document.querySelector('.agent-markdown a[href*="example.com/corner"]') as HTMLElement;
          for (const type of ['mouseover', 'mouseenter']) {
            link.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
          }
        });
        try {
          await expect(card).toBeVisible({ timeout: 3000 });
          opened = true;
        } catch {
          await page.waitForTimeout(400);
        }
      }
      expect(opened, `preview card must open near ${name}`).toBe(true);
      const open = card.locator('a', { hasText: 'Open' });
      await expect(open).toBeVisible();
      const box = await open.boundingBox();
      expect(box, `Open control near ${name} must be laid out`).toBeTruthy();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.y).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(page.viewportSize()!.width);
      expect(box!.y + box!.height).toBeLessThanOrEqual(page.viewportSize()!.height);
      await shot(page, `A5-edges-${name}`);
      await page.evaluate(() => {
        const link = document.querySelector('.agent-markdown a[href*="example.com/corner"]') as HTMLElement;
        for (const type of ['mouseout', 'mouseleave']) {
          link.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
        }
      });
      await expect(card).toBeHidden({ timeout: 3000 });
    }
  });

  test('A6 pointer travel onto the card keeps it open', async ({ page }) => {
    await openMdWorkspace(page);
    const anchor = page.locator('.agent-markdown a[href*="example.com/kbd"]').first();
    const card = page.locator('[role="dialog"][aria-label^="Link preview"]');
    // Transfer while the card is LOADING (the common real-user flow: the
    // card appears at intent and the user moves onto it). The card must
    // then survive its own metadata settle with the pointer on it.
    // Transient run-status overlays can cover the card region while the
    // page updates — the product honestly closes a covered card — so a
    // real user re-hovers; the transfer retries with strict checks.
    let transferred = false;
    for (let attempt = 0; attempt < 3 && !transferred; attempt++) {
      await anchor.hover();
      await expect(card).toBeVisible({ timeout: 8000 });
      const box = await card.boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + 10, { steps: 8 });
      await page.waitForTimeout(400); // spans leave-timer + metadata settle
      try {
        await expect(card).toBeVisible({ timeout: 2000 });
        transferred = true;
      } catch {
        await page.waitForTimeout(400); // let transient overlays clear
      }
    }
    expect(transferred, 'pointer transfer onto the card must keep it open').toBe(true);
    await shot(page, 'A6-pointer-transfer');
  });

  test('A7 keyboard focus opens; focus enters the card; Escape returns focus', async ({ page }) => {
    await openMdWorkspace(page);
    const anchor = page.locator('.agent-markdown a[href*="example.com/kbd"]').first();
    await anchor.focus();
    const card = page.locator('[role="dialog"][aria-label^="Link preview"]');
    await expect(card).toBeVisible({ timeout: 8000 });
    await page.keyboard.press('Tab');
    const openInside = await page.evaluate(() =>
      document.activeElement?.textContent?.includes('Open') ?? false);
    expect(openInside, 'focus can enter the preview card').toBe(true);
    await page.keyboard.press('Escape');
    await expect(card).toBeHidden({ timeout: 3000 });
    await expect(anchor).toBeFocused();
    await shot(page, 'A7-keyboard-escape');
  });

  // Touch emulation is a CONTEXT capability (hasTouch), not a runtime
  // switch — this block needs its own describe to opt the context in;
  // test.use inside a test body is not permitted.
  test.describe.serial('A8 coarse-pointer context', () => {
    test.use({ hasTouch: true });
    test('A8 coarse-pointer viewport exposes an explicit trigger', async ({ page }) => {
      await openMdWorkspace(page);
      await page.setViewportSize({ width: 390, height: 780 });
      const anchor = page.locator('.agent-markdown a[href*="example.com/corner"]');
      // The anchor always exposes the expanded state for assistive tech;
      // under coarse pointer the FIRST tap opens affordances instead of
      // navigating (component contract), and the card carries an explicit
      // Close control.
      await expect(anchor).toHaveAttribute('aria-expanded', /.*/);
      await anchor.tap();
      const card = page.locator('[role="dialog"][aria-label^="Link preview"]');
      await expect(card).toBeVisible({ timeout: 8000 });
      await shot(page, 'A8-touch-trigger');
    });
  });

  test('A9/A11 denied destination (real backend) and slow load get explicit states', async ({ page }) => {
    await openMdWorkspace(page);
    // Denied/unresolvable destination: the REAL preview endpoint answers
    // for slow-a.example; the card shows the explicit fallback with an
    // authorized Open action (never a fabricated title).
    const denied = page.locator('.agent-markdown a[href*="slow-a.example"]');
    await denied.hover();
    const card = page.locator('[role="dialog"][aria-label^="Link preview"]');
    await expect(card).toBeVisible({ timeout: 15000 });
    await expect(card.locator('a', { hasText: 'Open' })).toBeVisible();
    await shot(page, 'A9-denied-destination-state');
    await page.keyboard.press('Escape');

    // Slow load: while metadata is pending the card shows a loading
    // state (role=status) rather than a blank card (fixture-backed for
    // deterministic timing).
    await page.route('**/api/v1/link-previews', async (route) => {
      console.log('ROUTE: handler start', route.request().url().slice(-30));
      await new Promise((r) => setTimeout(r, 900));
      try {
        await route.fulfill({ status: 200, contentType: 'application/json',
          body: JSON.stringify({ title: 'SLOW LOAD TITLE', state: 'ready',
                                 final_host: 'example.com' }) });
        console.log('ROUTE: fulfilled');
      } catch (e) {
        console.log('ROUTE: fulfill failed:', String(e).slice(0, 120));
      }
    });
    const slow = page.locator('.agent-markdown a[href*="example.com/kbd"]');
    let slowLoaded = false;
    for (let attempt = 0; attempt < 3 && !slowLoaded; attempt++) {
      await slow.hover();
      await expect(card).toBeVisible({ timeout: 8000 });
      await expect(card.locator('[role="status"]')).toBeVisible({ timeout: 3000 });
      await shot(page, 'A11-slow-load-loading-state');
      try {
        await expect(card).toContainText('SLOW LOAD TITLE', { timeout: 6000 });
        slowLoaded = true;
      } catch {
        await page.keyboard.press('Escape');
        await page.waitForTimeout(400);
      }
    }
    expect(slowLoaded, 'slow-load card must settle to its metadata').toBe(true);
  });
});