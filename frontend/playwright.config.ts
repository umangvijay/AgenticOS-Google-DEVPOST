import { defineConfig, devices } from '@playwright/test';

const port = Number(process.env.E2E_PORT || 3000);
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid E2E_PORT');
const baseURL = `http://localhost:${port}`;

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  use: {
    baseURL,
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
    env: { PORT: String(port) },
    command: process.env.E2E_PRODUCTION === '1'
      ? 'HOSTNAME=127.0.0.1 node start-standalone.mjs'
      : 'npm run dev:fast',
    url: baseURL,
    // E2E_REUSE=1: the qualification harness points playwright at the
    // compose-served production image (the same artifact this command
    // would build) instead of failing on the occupied port.
    reuseExistingServer:
      (!process.env.CI && process.env.E2E_PRODUCTION !== '1')
      || process.env.E2E_REUSE === '1',
    timeout: 120 * 1000,
  },
});
