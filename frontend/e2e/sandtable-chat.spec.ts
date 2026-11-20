import { test, expect, type Page } from '@playwright/test';
import type { WorkflowRun } from '../src/lib/api';

const user = { id: 'chat-test', email: 'chat@example.invalid', name: 'Chat tester', role: 'user', is_active: true };
function savedRun(id: string, goal: string, reply: string): WorkflowRun {
  return { run_id: id, workflow_id: `wf-${id}`, user_id: user.id, goal, status: 'COMPLETED',
    thread_id: 'first', created_at: '2026-11-19T12:00:00Z', tasks: [{ task_id: `task-${id}`,
      run_id: id, workflow_id: `wf-${id}`, agent: 'core.chat', status: 'COMPLETED',
      input_data: {}, dependencies: [], attempt: 1, output_data: { reply } }] };
}

async function fixture(page: Page, history: WorkflowRun[]) {
  await page.addInitScript(u => localStorage.setItem('agentos_auth', JSON.stringify({
    accessToken: 'local-test-token', refreshToken: 'local-test-refresh', user: u,
  })), user);
  // Never reach provider or live infrastructure endpoints during UI tests.
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/health') return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ llm_status: { status: 'vertex' } }) });
    if (url.origin === `http://localhost:${Number(process.env.E2E_PORT || 3000)}`) return route.continue();
    return route.abort();
  });
  const runs = new Map(history.map(run => [run.run_id, run]));
  let releaseSubmission: (() => void) | undefined;
  let releaseEvents: (() => void) | undefined;
  let holdSubmission = false, holdEvents = false;
  let submissions = 0;
  const requests: string[] = [];
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    requests.push(path);
    const json = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
    if (path === '/auth/me') return json(user);
    if (path === '/health') return json({ llm_status: { status: 'vertex' } });
    if (path === '/settings') return json({ settings: { default_model: 'gemini-3.8-flash' },
      available_models: ['gemini-3.8-flash', 'gemini-4-argon'],
      model_catalog: { default_model: 'gemini-3.8-flash', models: [
        { id: 'gemini-3.8-flash', name: 'Gemini 3.8 Flash', family: 'Gemini 3', selectable: true },
        { id: 'gemini-4-argon', name: 'Gemini 4 Argon', family: 'Gemini 4', selectable: true },
        { id: 'retired-model', name: 'Retired model', family: 'Retired', selectable: false },
      ] } });
    if (path === '/browser/sessions') return json({ sessions: [] });
    if (path === '/workflows' && route.request().method() === 'POST') {
      submissions++;
      if (holdSubmission) await new Promise<void>(resolve => { releaseSubmission = resolve; });
      const { goal } = route.request().postDataJSON();
      runs.set('followup', savedRun('followup', goal, 'The follow-up answer is saved.'));
      return json({ run_id: 'followup', workflow_id: 'wf-followup', status: 'RUNNING', thread_id: 'first' });
    }
    if (path === '/workflows') return json({ workflows: history, count: history.length });
    if (path.endsWith('/thread')) return json({ thread_id: 'first', workflows: [...runs.values()] });
    if (path.endsWith('/events')) {
      const id = path.split('/')[2];
      if (holdEvents) await new Promise<void>(resolve => { releaseEvents = resolve; });
      const event = { event_id: `${id}-completed`, type: 'WORKFLOW_COMPLETED', run_id: id,
        workflow_id: `wf-${id}`, summary: 'Workflow completed', timestamp: '2026-11-19T12:00:01Z', sanitized_metadata: {} };
      return route.fulfill({ contentType: 'text/event-stream', body: `id: ${event.event_id}\ndata: ${JSON.stringify(event)}\n\n` });
    }
    if (/^\/workflows\/[^/]+$/.test(path)) return json(runs.get(path.split('/')[2]));
    if (path === '/notifications') return json({ notifications: [], unread_count: 0 });
    return json({});
  });
  return { runs, requests, holdSubmit: () => { holdSubmission = true; },
    releaseSubmit: () => releaseSubmission?.(), holdStream: () => { holdEvents = true; },
    releaseStream: () => { holdEvents = false; releaseEvents?.(); }, submissions: () => submissions };
}

test('completion recovers saved output, follow-ups appear before acceptance, and details stay out of chat', async ({ page }) => {
  const completed = savedRun('first', 'What can you do?', 'I can help with your work.');
  const pending = { ...completed, status: 'RUNNING', tasks: completed.tasks.map(t => ({ ...t, status: 'PENDING', output_data: undefined })) };
  const f = await fixture(page, [pending]);
  f.holdStream(); f.holdSubmit();
  await page.goto('/dashboard/workspace/first');
  await expect(page.getByText('Thinking…', { exact: true })).toBeVisible();
  // Reproduce a completion event with NO task output in its metadata.
  f.runs.set('first', completed); f.releaseStream();
  await expect(page.getByText('I can help with your work.', { exact: true })).toBeVisible();
  await expect(page.getByText('Thinking…', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Token budget', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Feedback on this run', { exact: true })).toHaveCount(0);
  expect(f.requests.some(path => path.startsWith('/usage') || path.startsWith('/feedback'))).toBe(false);
  await expect(page.getByRole('combobox', { name: 'Model for this message' })).toHaveValue('gemini-3.8-flash');
  await expect(page.locator('option[value="gemini-4-argon"]')).toHaveCount(1);
  await expect(page.locator('option[value="retired-model"]')).toBeDisabled();
  await page.getByRole('textbox', { name: 'Message', exact: true }).fill('My next message');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  const conversation = page.getByRole('region', { name: 'Conversation' });
  await expect(conversation.getByText('My next message', { exact: true })).toBeVisible();
  await expect(conversation.getByText('Sending…', { exact: true })).toBeVisible();
  await expect.poll(() => f.submissions()).toBe(1);
  f.releaseSubmit();
  await expect(conversation.getByText('The follow-up answer is saved.', { exact: true })).toBeVisible();
  await expect(conversation.getByText('My next message', { exact: true })).toHaveCount(1);
  if (await page.getByRole('button', { name: 'Execution', exact: true }).getAttribute('aria-expanded') !== 'true') await page.getByRole('button', { name: 'Execution', exact: true }).click();
  const panel = page.getByRole('complementary', { name: 'Execution details' });
  await expect(panel).toBeVisible();
  const panelBounds = (await panel.boundingBox())!;
  const chatBounds = (await conversation.boundingBox())!;
  expect(panelBounds.x).toBeGreaterThanOrEqual(chatBounds.x + chatBounds.width);
  expect(panelBounds.height).toBeLessThan(page.viewportSize()!.height * 0.8);
  await panel.locator('.execution-section > summary').filter({ hasText: 'Activity' }).click();
  await panel.locator('.execution-step > summary').filter({ hasText: 'Run activity' }).click();
  await expect(panel.getByText('Workflow completed').first()).toBeVisible();
  await page.screenshot({ path: '/tmp/sandtable-chat-desktop.png', fullPage: true });
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
  await page.screenshot({ path: '/tmp/sandtable-chat-dark.png', fullPage: true });
  await page.getByRole('button', { name: 'Close execution' }).click();
  await expect(panel).toHaveCount(0);
  // Reload proves both messages are persisted, rather than just optimistic UI.
  await page.reload();
  await expect(conversation.getByText('What can you do?', { exact: true })).toBeVisible();
  await expect(conversation.getByText('My next message', { exact: true })).toBeVisible();
  await expect(conversation.getByText('The follow-up answer is saved.', { exact: true })).toBeVisible();
});

for (const mobile of [false, true]) {
  test(`conversation scrolls independently and preserves reading position (${mobile ? 'mobile' : 'desktop'})`, async ({ page }) => {
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 900 });
    const history = Array.from({ length: 15 }, (_, i) => savedRun(i ? `turn-${i}` : 'first',
      `Question number ${i + 1}`, Array(10).fill(`Response for question ${i + 1}.`).join('\n\n')));
    const last = history[history.length - 1];
    last.status = 'RUNNING'; last.tasks = [];
    const f = await fixture(page, history); f.holdStream();
    await page.goto('/dashboard/workspace/first');
    const conversation = page.getByRole('region', { name: 'Conversation' });
    await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toBeVisible();
    await expect.poll(() => conversation.evaluate(el => el.scrollHeight > el.clientHeight)).toBe(true);
    await conversation.evaluate(el => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll')); });
    await expect(conversation.getByText('Question number 1', { exact: true })).toBeVisible();
    const final = savedRun(last.run_id, last.goal, 'Final answer arrived while reading.');
    f.runs.set(last.run_id, final); f.releaseStream();
    await expect(conversation.getByText('Final answer arrived while reading.', { exact: true })).toHaveCount(1);
    expect(await conversation.evaluate(el => el.scrollTop)).toBe(0);
    await page.getByRole('button', { name: 'Jump to latest' }).click();
    await expect(conversation.getByText('Final answer arrived while reading.', { exact: true })).toBeVisible();
    const bounds = (await page.getByRole('textbox', { name: 'Message', exact: true }).boundingBox())!;
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(mobile ? 844 : 900);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (await page.getByRole('button', { name: 'Execution', exact: true }).getAttribute('aria-expanded') !== 'true') await page.getByRole('button', { name: 'Execution', exact: true }).click();
    await expect(page.getByRole('complementary', { name: 'Execution details' })).toBeVisible();
    await page.screenshot({ path: mobile ? '/tmp/sandtable-chat-mobile.png' : '/tmp/sandtable-chat-scroll.png', fullPage: true });
    await page.getByRole('button', { name: 'Close execution' }).click();
    await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toBeVisible();
    if (mobile) await page.screenshot({ path: '/tmp/sandtable-chat-mobile-conversation.png', fullPage: true });
  });
}

for (const status of ['COMPLETED', 'CANCELLED', 'FAILED', 'NEEDS_REVIEW', 'EXPIRED']) {
  test(`${status} with stale pending tasks never leaves the assistant thinking`, async ({ page }) => {
    const run = savedRun('first', 'My message must stay visible', '');
    run.status = status;
    run.tasks[0].status = 'PENDING';
    run.tasks[0].output_data = undefined;
    await fixture(page, [run]);
    await page.goto('/dashboard/workspace/first');
    const conversation = page.getByRole('region', { name: 'Conversation' });
    await expect(conversation.getByText('My message must stay visible', { exact: true })).toBeVisible();
    await expect(conversation.getByText('Thinking…', { exact: true })).toHaveCount(0);
    await expect(conversation.getByRole('status')).toContainText(status === 'COMPLETED' ? 'without a saved reply'
      : status === 'CANCELLED' ? 'stopped' : status === 'FAILED' ? 'failed' : status === 'EXPIRED' ? 'expired' : 'needs review');
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
  });
}

test('a failed submission keeps the text and retries with the same submission identity', async ({ page }) => {
  await fixture(page, [savedRun('first', 'Hello', 'Hello back')]);
  const keys: (string | undefined)[] = [];
  await page.route('**/api/v1/workflows', async route => {
    if (route.request().method() !== 'POST') return route.fallback();
    keys.push(route.request().headers()['idempotency-key']);
    if (keys.length === 1) return route.abort();
    return route.fallback();
  });
  await page.goto('/dashboard/workspace/first');
  const input = page.getByRole('textbox', { name: 'Message', exact: true });
  await input.fill('Keep this draft after a network failure');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByText(/Could not reach the API/)).toBeVisible();
  await expect(input).toHaveValue('Keep this draft after a network failure');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByText('The follow-up answer is saved.', { exact: true })).toBeVisible();
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBeTruthy();
  expect(keys[1]).toBe(keys[0]);
});

test('Codex card and nested steps expand independently, preserve state, and dismiss by keyboard', async ({ page }) => {
  const first = savedRun('first', 'First message', 'First saved response');
  first.tasks[0].input_data = { url: 'https://example.com/report' };
  const second = savedRun('second', 'Second message', 'Second saved response');
  const f = await fixture(page, [first, second]);
  await page.goto('/dashboard/workspace/first');
  const trigger = page.getByRole('button', { name: 'Execution', exact: true });
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');
  const panel = page.getByRole('complementary', { name: 'Execution details' });
  await expect(panel.getByRole('link', { name: 'Assistant response' })).toBeVisible();
  expect(f.requests.some(p => /evaluations|commands\/by-run|effects\/by-run/.test(p))).toBe(false);
  const activity = panel.locator('.execution-section').filter({ has: page.locator(':scope > summary', { hasText: /^Activity/ }) });
  await activity.locator(':scope > summary').click();
  const step = panel.locator('.execution-step').filter({ hasText: 'Respond to your message' });
  await step.locator('summary').click();
  await expect(step).toHaveAttribute('open', '');
  await page.getByRole('button', { name: 'Collapse execution card' }).click();
  await expect(page.getByRole('button', { name: 'Expand execution card' })).toHaveAttribute('aria-expanded', 'false');
  expect((await panel.boundingBox())!.height).toBeLessThan(90);
  await page.getByRole('button', { name: 'Expand execution card' }).click();
  await expect(step).toHaveAttribute('open', '');
  await panel.getByRole('combobox', { name: 'Execution for message' }).selectOption('first');
  await expect(panel.getByRole('link', { name: 'example.com/report' })).toHaveAttribute('href', 'https://example.com/report');
  await panel.getByRole('link', { name: 'Assistant response' }).click();
  await expect(page.getByText('First saved response', { exact: true })).toBeInViewport();
  await page.keyboard.press('Escape');
  await expect(panel).toBeHidden();
  await expect(trigger).toBeFocused();
  await trigger.press('Enter');
  await expect(panel).toBeVisible();
  await expect(panel.getByRole('combobox', { name: 'Execution for message' })).toHaveValue('first');
  // Run diagnostics are still available, but fetched only after opening them.
  await panel.locator('.execution-inspect > summary').click();
  await expect.poll(() => f.requests.some(p => p.includes('/commands/by-run/first'))).toBe(true);
  await page.screenshot({ path: '/tmp/sandtable-codex-card-expanded.png', fullPage: true });
});

test('composer grows for multiline drafts and restores the text after refresh', async ({ page }) => {
  await fixture(page, [savedRun('first', 'Hello', 'How can I help?')]);
  await page.goto('/dashboard/workspace/first');
  const composer = page.getByRole('textbox', { name: 'Message', exact: true });
  await expect(composer).toBeVisible();
  const height = (await composer.boundingBox())!.height;
  const draft = 'First line\nSecond line\nThird line\nFourth line';
  await composer.fill(draft);
  await expect.poll(async () => (await composer.boundingBox())!.height).toBeGreaterThan(height);
  await page.reload();
  await expect(composer).toHaveValue(draft);
  await composer.evaluate((el: HTMLTextAreaElement) => el.setSelectionRange(el.value.length, el.value.length));
  await composer.press('Shift+Enter');
  await expect(composer).toHaveValue(draft + '\n');
  await expect(page.getByRole('region', { name: 'Conversation' }).getByText(draft)).toHaveCount(0);
});
