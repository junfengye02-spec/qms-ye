import { defineConfig, devices } from '@playwright/test';
export default defineConfig({
  testDir: './tests',
  testMatch: 'pui_contract_warranty.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 45000,
  projects: [{ name: 'native-uuid' }, { name: 'lan-http' }],
  use: {
    ...devices['Desktop Chrome'],
    baseURL: 'http://127.0.0.1:5181',
    headless: true
  },
  webServer: {
    command:
      'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5181 --strictPort',
    url: 'http://127.0.0.1:5181/tests/fixtures/contract-warranty/index.html',
    reuseExistingServer: false
  }
});
