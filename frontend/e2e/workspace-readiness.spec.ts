import { test, expect } from '@playwright/test';
// Rendering regression fixtures; live backend smoke tests are separate.
const run = {
  run_id:'regression-run', workflow_id:'wf-test', user_id:'test-user',
  goal:'Check the public API', status:'COMPLETED', created_at:'2026-10-10T08:00:00Z',
  tasks:[{task_id:'health-1',workflow_id:'wf-test',run_id:'regression-run',agent:'core.health',
    status:'COMPLETED',input_data:{url:'https://example.com'},output_data:{status_code:200,data:null},dependencies:[],attempt:1}],
};
test('completed thread handles null output and invalid event timestamp', async ({page}) => {
  const errors: string[] = [];
  page.on('pageerror',error => errors.push(error.message));
  await page.addInitScript(() => localStorage.setItem('agentos_auth',JSON.stringify({
    accessToken:'test-only-token',refreshToken:'test-only-refresh',user:{id:'test-user',email:'test@example.invalid',name:'Test',role:'user',is_active:true},
  })));
  await page.route('**/api/v1/**',async route => {
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith('/events')) {
      await route.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({event_id:'e1',type:'TASK_COMPLETED',timestamp:'invalid',summary:'Probe completed',workflow_id:run.workflow_id,run_id:run.run_id})+'\n\n'});return;
    }
    const body=path.endsWith('/thread')?{thread_id:run.run_id,workflows:[run]}:
      path.endsWith('/workflows/regression-run')?run:
      path.endsWith('/workflows')?{workflows:[run],total:1}:
      path.endsWith('/settings')?{settings:{default_model:'gemini-2.5-pro'},available_models:['gemini-2.5-pro']}:
      path.includes('notifications')?{notifications:[],unread_count:0}:{};
    await route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto('/dashboard/workspace/regression-run');
  // Round 5 (audit bug 6): the assistant line reports the full check, not just 'HTTP 200'.
  await expect(page.getByText('HTTP 200', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Model for this message')).toHaveValue('gemini-2.5-pro');
  await expect(page.getByText("This page couldn't load")).toHaveCount(0);
  expect(errors).toEqual([]);
});
