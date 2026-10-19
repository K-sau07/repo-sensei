/**
 * Switch the mock API into a situation worth testing, from the browser:
 *   localStorage.setItem("sensei.mock.scenario", "no-orgs")
 * MSW runs its handlers in the page, so they can read this. Unset means the normal signed-in user.
 */
export type MockScenario = "signed-in" | "signed-out" | "expired" | "no-orgs";

export const SCENARIO_KEY = "sensei.mock.scenario";

export function mockScenario(): MockScenario {
  if (typeof localStorage === "undefined") return "signed-in";
  const value = localStorage.getItem(SCENARIO_KEY);
  return value === "signed-out" || value === "expired" || value === "no-orgs" ? value : "signed-in";
}
