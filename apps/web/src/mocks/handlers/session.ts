import { db, FRIEND, HUBOT, OCTO, type MockUser } from "@/mocks/db";
import { mockScenario } from "@/mocks/scenario";

import { apiError } from "./errors";

/** The signed-in mock user, or the 401 the API would send. */
export function currentUser(): MockUser | Response {
  const scenario = mockScenario();
  if (scenario === "signed-out") return apiError(401, "unauthenticated", "Sign in to continue.");
  if (scenario === "expired") {
    return apiError(401, "session_expired", "Your session has expired. Please sign in again.");
  }
  const id = scenario === "as-friend" ? FRIEND : scenario === "as-member" ? HUBOT : OCTO;
  const user = db.users.find((u) => u.id === id);
  if (!user) throw new Error("mock seed is missing a user");
  return user;
}

export function insufficientRole(required: string, yours: string) {
  return apiError(403, "insufficient_role", `This needs the ${required} role.`, {
    required_role: required,
    your_role: yours,
  });
}
