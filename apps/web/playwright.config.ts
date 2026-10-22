import { defineConfig } from "@playwright/test";

// The suite runs against the dev server in mock mode, so it needs no backend.
const port = Number(process.env.E2E_PORT ?? 3100);
const baseURL = `http://localhost:${port}`;
const chromium = { browserName: "chromium", viewport: { width: 1280, height: 800 } } as const;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: { baseURL, trace: "on-first-retry" },
  // Shared CI runners compile routes on demand under parallel load; give pages longer to settle.
  expect: { timeout: process.env.CI ? 10_000 : 5_000 },
  // No device preset: its spoofed Windows user agent would make the app expect Ctrl where the
  // real platform sends ⌘ (and vice versa), breaking the shortcut tests.
  projects: [
    { name: "chromium", use: chromium, grepInvert: /@perf/ },
    // Timing tests (tagged @perf) run after the rest, on one worker, so no other test steals
    // their frames.
    {
      name: "perf",
      use: chromium,
      grep: /@perf/,
      dependencies: ["chromium"],
      fullyParallel: false,
    },
  ],
  webServer: {
    command: `pnpm exec next dev --port ${port}`,
    url: `${baseURL}/login`,
    env: { NEXT_PUBLIC_MOCKS: "1" },
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
