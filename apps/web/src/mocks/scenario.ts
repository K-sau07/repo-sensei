/**
 * Switch the mock API into a situation worth testing, from the browser:
 *   localStorage.setItem("sensei.mock.scenario", "no-orgs")
 * MSW runs its handlers in the page, so they can read this. Unset means the normal signed-in user
 * (Octo, owner of Acme). "as-friend" signs in as a second user (friend@example.com), to accept
 * invitations; "as-member" as Hubot, a plain member of Acme, to see what non-admins get.
 */
export type MockScenario =
  "signed-in" | "signed-out" | "expired" | "no-orgs" | "as-friend" | "as-member";

export const SCENARIO_KEY = "sensei.mock.scenario";

const SCENARIOS = new Set<string>(["signed-out", "expired", "no-orgs", "as-friend", "as-member"]);

export function mockScenario(): MockScenario {
  if (typeof localStorage === "undefined") return "signed-in";
  const value = localStorage.getItem(SCENARIO_KEY);
  return value !== null && SCENARIOS.has(value) ? (value as MockScenario) : "signed-in";
}
