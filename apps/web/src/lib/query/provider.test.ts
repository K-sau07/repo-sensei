import { describe, expect, it } from "vitest";

import { $api } from "@/lib/api/client";
import { ApiError, networkError } from "@/lib/api/errors";

import { queryKeys } from "./keys";
import { loginUrlFor, makeQueryClient, shouldRetry } from "./provider";

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
