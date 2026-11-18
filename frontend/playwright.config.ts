import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  use: {
    baseURL: 'http://localhost:3000',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    // Run against the actual production artifact when requested by the release gate.
    command: process.env.E2E_PRODUCTION === '1'
      ? 'HOSTNAME=127.0.0.1 PORT=3000 node .next/standalone/frontend/server.js'
      : 'npm run dev:fast',
    url: 'http://localhost:3000',
    // E2E_REUSE=1: the qualification harness points playwright at the
    // compose-served production image (the same artifact this command
    // would build) instead of failing on the occupied port.
    reuseExistingServer:
      (!process.env.CI && process.env.E2E_PRODUCTION !== '1')
      || process.env.E2E_REUSE === '1',
    timeout: 120 * 1000,
  },
});
