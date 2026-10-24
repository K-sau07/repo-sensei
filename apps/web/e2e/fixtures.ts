import { test as base, expect, type BrowserContext } from "@playwright/test";

/** A session cookie as the API sets it in development (the mock API accepts any session). */
export async function signIn(context: BrowserContext, baseURL: string) {
  await context.addCookies([{ name: "session", value: "e2e-session", url: baseURL }]);
}

/** Pick a mock API situation (see src/mocks/scenario.ts) before the page loads. */
export async function useMockScenario(context: BrowserContext, scenario: string) {
  await context.addInitScript((value) => {
    localStorage.setItem("sensei.mock.scenario", value);
  }, scenario);
}

/** Pages that need a session get one unless a test opts out with `signedIn: false`. */
export const test = base.extend<{ signedIn: boolean }>({
  signedIn: [true, { option: true }],
  // (Playwright's fixture callback; named `provide` so lint doesn't take it for React's `use`.)
  context: async ({ context, signedIn, baseURL }, provide) => {
    if (signedIn && baseURL) await signIn(context, baseURL);
    await provide(context);
  },
});

/**
 * A timing budget in ms. The budgets are the product's, met on a developer machine. CI's shared
 * runners (2 CPUs, no GPU) are 3-5x slower and vary from run to run, so there a budget is
 * `CI_PERF_SLACK` times as long: still short enough to catch what matters (a freeze, a lost
 * virtualisation), not the runner's noise. The measured numbers are logged either way.
 */
export const CI_PERF_SLACK = 4;
export const budget = (ms: number) => (process.env.CI ? ms * CI_PERF_SLACK : ms);

export { expect };
