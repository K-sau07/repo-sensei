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

export { expect };
