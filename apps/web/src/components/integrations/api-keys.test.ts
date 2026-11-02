import { describe, expect, it } from "vitest";

import type { ApiKey } from "@/lib/api/types";

import { keyState, splitKeys } from "./api-keys-settings";

const NOW = Date.parse("2026-11-01T12:00:00Z");
const key = (over: Partial<ApiKey>): ApiKey => ({
  id: "k",
  name: "k",
  prefix: "AbCd1234",
  scopes: ["read:repo"],
  owner_login: "octo",
  mine: true,
  created_at: "2026-10-01T12:00:00Z",
  last_used_at: null,
  expires_at: null,
  revoked_at: null,
  ...over,
});

describe("API key states", () => {
  it("a key works until it is revoked or reaches its expiry", () => {
    expect(keyState(key({}), NOW)).toBe("active");
    expect(keyState(key({ expires_at: "2026-11-02T00:00:00Z" }), NOW)).toBe("active");
    expect(keyState(key({ expires_at: "2026-11-01T12:00:00Z" }), NOW)).toBe("expired");
    // Revoked says more than expired.
    expect(
      keyState(
        key({ expires_at: "2026-10-01T00:00:00Z", revoked_at: "2026-09-30T00:00:00Z" }),
        NOW,
      ),
    ).toBe("revoked");
  });

  it("splits working keys from the rest, keeping the API's order", () => {
    const keys = [
      key({ id: "a" }),
      key({ id: "b", revoked_at: "2026-10-02T00:00:00Z" }),
      key({ id: "c" }),
      key({ id: "d", expires_at: "2026-10-31T00:00:00Z" }),
    ];
    const { working, past } = splitKeys(keys, NOW);
    expect(working.map((k) => k.id)).toEqual(["a", "c"]);
    expect(past.map((k) => k.id)).toEqual(["b", "d"]);
  });
});
