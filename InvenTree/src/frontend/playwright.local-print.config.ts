import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  testMatch: 'pui_local_print.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 30000,
  use: {
    ...devices['Desktop Chrome'],
    baseURL: 'http://127.0.0.1:5177',
    headless: true
  },
  webServer: {
    command:
      'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5177 --strictPort',
    url: 'http://127.0.0.1:5177/tests/fixtures/local-print/index.html',
    reuseExistingServer: false
  }
});
