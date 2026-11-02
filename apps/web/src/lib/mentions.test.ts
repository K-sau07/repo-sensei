import { describe, expect, it } from "vitest";

import { mentionAt } from "./mentions";

describe("a mention", () => {
  it("is the @ being typed before the caret", () => {
    expect(mentionAt("what calls @crea", 16)).toEqual({ start: 11, query: "crea" });
    expect(mentionAt("@", 1)).toEqual({ start: 0, query: "" });
    expect(mentionAt("mail me@example.com", 19)).toBeNull(); // not after a space
    expect(mentionAt("what calls @crea and", 20)).toBeNull(); // the caret moved past it
  });
});
