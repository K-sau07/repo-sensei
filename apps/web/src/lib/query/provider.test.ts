import { afterEach, describe, expect, it, vi } from "vitest";

import { $api } from "@/lib/api/client";
import { ApiError, networkError } from "@/lib/api/errors";
import { historyKey } from "@/lib/palette/history";

import { queryKeys } from "./keys";
import { loginUrlFor, makeQueryClient, sessionExpired, shouldRetry } from "./provider";

const apiError = (status: number) => new ApiError({ status, code: "x", message: "x" });

describe("shouldRetry", () => {
  it.each([400, 401, 403, 404, 409, 422])("never retries %i", (status) => {
    expect(shouldRetry(0, apiError(status))).toBe(false);
  });

  it("retries server and network errors twice", () => {
    for (const error of [apiError(500), apiError(503), networkError(new TypeError("offline"))]) {
      expect([0, 1, 2].map((count) => shouldRetry(count, error))).toEqual([true, true, false]);
    }
  });
});

describe("query client defaults", () => {
  it("keeps data fresh for 30 seconds and does not retry mutations", () => {
    const defaults = makeQueryClient().getDefaultOptions();
    expect(defaults.queries?.staleTime).toBe(30_000);
    expect(defaults.mutations?.retry).toBe(false);
  });
});

describe("loginUrlFor", () => {
  it("keeps where the user was", () => {
    expect(loginUrlFor({ pathname: "/acme/langflow/ask", search: "?q=checkout" })).toBe(
      "/login?next=%2Facme%2Flangflow%2Fask%3Fq%3Dcheckout",
    );
  });

  it("does nothing on the login page itself", () => {
    expect(loginUrlFor({ pathname: "/login", search: "?error=oauth_failed" })).toBeNull();
  });
});

describe("queryKeys", () => {
  it("match the keys the generated hooks use", () => {
    expect(queryKeys.me()).toEqual(["get", "/api/v1/me"]);
    expect(queryKeys.members("acme")).toEqual(
      $api.queryOptions("get", "/api/v1/orgs/{org}/members", {
        params: { path: { org: "acme" } },
      }).queryKey,
    );
  });
});

describe("sessionExpired", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function browser(pathname: string) {
    const values = new Map([
      [historyKey("user-1"), "[]"],
      ["sensei.shell.v1", "{}"],
    ]);
    const assign = vi.fn();
    vi.stubGlobal("window", {
      location: { pathname, search: "", assign },
      setTimeout: globalThis.setTimeout,
      localStorage: {
        get length() {
          return values.size;
        },
        key: (index: number) => [...values.keys()][index] ?? null,
        removeItem: (key: string) => values.delete(key),
      },
    });
    return { values, assign };
  }

  it("forgets the palette's history, then goes to sign-in keeping where they were", async () => {
    vi.useFakeTimers();
    const { values, assign } = browser("/acme/api/code");
    sessionExpired();
    expect([...values.keys()]).toEqual(["sensei.shell.v1"]);
    await vi.runAllTimersAsync();
    expect(assign).toHaveBeenCalledWith("/login?next=%2Facme%2Fapi%2Fcode");
  });

  it("forgets it on the sign-in page too (where it doesn't redirect)", () => {
    const { values, assign } = browser("/login");
    sessionExpired();
    expect(values.has(historyKey("user-1"))).toBe(false);
    expect(assign).not.toHaveBeenCalled();
  });
});
