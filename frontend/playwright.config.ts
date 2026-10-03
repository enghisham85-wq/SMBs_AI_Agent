import { defineConfig, devices } from "@playwright/test";

// Own ports and DB so it never touches a running demo.
const API_PORT = 8765;
const WEB_PORT = 5175;
const backendEnv = [
  "DATABASE_URL=sqlite+aiosqlite:///./var/e2e/app.db",
  "CHECKPOINT_DB_PATH=./var/e2e/checkpoints.db",
  "FILES_DIR=./var/e2e/files",
  "SAMPLE_INVOICES_DIR=./var/e2e/sample_invoices",
  "DEMO_MODE=true",
  "LLM_MODE=offline",
  "TELEGRAM_BOT_TOKEN=",
  "APP_ENV=test",
];

export default defineConfig({
  testDir: "tests/e2e",
  timeout: 120_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: { baseURL: `http://localhost:${WEB_PORT}`, trace: "retain-on-failure" },
  // PW_CHANNEL=chrome|msedge if Playwright's browser can't be downloaded.
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"], channel: process.env.PW_CHANNEL || undefined } },
  ],
  webServer: [
    {
      command: `node tests/e2e/serve-backend.mjs ${API_PORT} ${backendEnv.map((e) => JSON.stringify(e)).join(" ")}`,
      url: `http://localhost:${API_PORT}/api/v1/openapi.json`,
      timeout: 240_000,
      reuseExistingServer: false,
    },
    {
      command: `npx vite --port ${WEB_PORT} --strictPort`,
      url: `http://localhost:${WEB_PORT}`,
      env: { API_TARGET: `http://localhost:${API_PORT}`, VITE_PORT: String(WEB_PORT) },
      timeout: 120_000,
      reuseExistingServer: false,
    },
  ],
});
