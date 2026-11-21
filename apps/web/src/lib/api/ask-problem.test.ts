import { describe, expect, it } from "vitest";

import { askProblem } from "./ask";

describe("askProblem", () => {
  it("reads the API's error envelope the chat transport hands over", () => {
    const body = JSON.stringify({
      error: { code: "trial_limit_reached", message: "The free trial covers 2 questions." },
    });
    expect(askProblem(new Error(body))).toEqual({
      code: "trial_limit_reached",
      message: "The free trial covers 2 questions.",
    });
  });

  it("keeps any other error's words", () => {
    expect(askProblem(new Error("Failed to fetch"))).toEqual({
      code: null,
      message: "Failed to fetch",
    });
  });
});
