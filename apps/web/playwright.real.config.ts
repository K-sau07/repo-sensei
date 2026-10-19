import { defineConfig } from "@playwright/test";

/**
 * Browser tests against the real API (no mocks): our FastAPI on the `sensei_e2e` database and a
 * GitHub OAuth stub, so the whole sign-in runs for real. Needs `make up`; `make e2e-real` prepares
 * the database first. Ports are their own, so a running dev setup is never touched.
 */
const STUB = 8099;
const API = 8010;
const WEB = 3210;
const web = `http://localhost:${WEB}`;
const repoRoot = "../..";

export default defineConfig({
  testDir: "./e2e-real",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: { baseURL: web, trace: "retain-on-failure" },
  projects: [
    { name: "real-api", use: { browserName: "chromium", viewport: { width: 1280, height: 800 } } },
  ],
  webServer: [
    {
      command: `uv run python scripts/e2e/github_oauth_stub.py --port ${STUB}`,
      cwd: repoRoot,
      url: `http://localhost:${STUB}/user`,
      ignoreHTTPSErrors: true,
      reuseExistingServer: false,
      // /user answers 401 without a token, which still proves the server is up.
      timeout: 60_000,
    },
    {
      command: `uv run uvicorn sensei_api.app:create_app --factory --port ${API}`,
      cwd: repoRoot,
      url: `http://localhost:${API}/api/v1/healthz`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        SENSEI_ENV: "dev",
        SENSEI_WEB_ORIGIN: web,
        SENSEI_DATABASE_URL:
          "postgresql+asyncpg://sensei_app:sensei_app@localhost:55432/sensei_e2e",
        SENSEI_GITHUB_CLIENT_ID: "Iv1.e2e-stub",
        SENSEI_GITHUB_CLIENT_SECRET: "e2e-stub-secret",
        SENSEI_GITHUB_WEB_URL: `http://localhost:${STUB}`,
        SENSEI_GITHUB_API_URL: `http://localhost:${STUB}`,
      },
    },
    {
      command: `pnpm exec next dev --port ${WEB}`,
      url: `${web}/login`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: { SENSEI_API_PROXY_ORIGIN: `http://localhost:${API}`, NEXT_PUBLIC_MOCKS: "0" },
    },
  ],
});
