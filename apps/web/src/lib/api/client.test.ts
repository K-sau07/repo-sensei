import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { resetMockDb } from "@/mocks/db";
import { server } from "@/mocks/node";

import { onAuthExpired } from "./auth-events";
import { createApiClient, readCsrfCookie } from "./client";
import { ApiError } from "./errors";

const BASE = "http://api.test";
const seen: Request[] = [];

beforeAll(() => {
  server.listen({ onUnhandledFrame: "error" });
  server.events.on("request:start", ({ request }) => {
    seen.push(request.clone());
  });
});
afterEach(() => {
  server.resetHandlers();
  resetMockDb();
  seen.length = 0;
});
afterAll(() => {
  server.close();
});

function client(csrf: string | null = "csrf-token-1") {
  return createApiClient({
    baseUrl: BASE,
    getCsrfToken: () => csrf,
    newRequestId: () => "req-1",
  });
}

describe("request middleware", () => {
  it("adds a request id to every call and no CSRF header to safe methods", async () => {
    const { data } = await client().GET("/api/v1/me");
    expect(data?.login).toBe("octo");
    const [request] = seen;
    expect(request?.headers.get("x-request-id")).toBe("req-1");
    expect(request?.headers.has("x-csrf-token")).toBe(false);
  });

  it.each(["POST", "PATCH", "DELETE"] as const)("sends the CSRF token on %s", async (method) => {
    const api = client();
    if (method === "POST") {
      await api.POST("/api/v1/orgs", { body: { name: "Beta", slug: "beta" } });
    } else if (method === "PATCH") {
      await api.PATCH("/api/v1/me", { body: { last_org_slug: "acme" } });
    } else {
      await api.DELETE("/api/v1/orgs/{org}/members/{user_id}", {
        params: { path: { org: "acme", user_id: "0199f000-0000-7000-8000-000000000002" } },
      });
    }
    expect(seen.at(-1)?.headers.get("x-csrf-token")).toBe("csrf-token-1");
  });

  it("omits the CSRF header when there is no cookie (the API answers csrf_failed)", async () => {
    await client(null).POST("/api/v1/orgs", { body: { name: "Gamma", slug: "gamma" } });
    expect(seen.at(-1)?.headers.has("x-csrf-token")).toBe(false);
  });
});

describe("errors", () => {
  it("turns the error envelope into an ApiError", async () => {
    server.use(
      http.get("*/api/v1/orgs/:org", () =>
        HttpResponse.json(
          {
            error: {
              code: "insufficient_role",
              message: "This needs the admin role.",
              request_id: "req-9",
              details: { required_role: "admin", your_role: "viewer" },
            },
          },
          { status: 403 },
        ),
      ),
    );
    const error = await client()
      .GET("/api/v1/orgs/{org}", { params: { path: { org: "acme" } } })
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 403,
      code: "insufficient_role",
      message: "This needs the admin role.",
      requestId: "req-9",
      details: { required_role: "admin", your_role: "viewer" },
      isClientError: true,
    });
  });

  it("handles responses that are not the envelope", async () => {
    server.use(
      http.get(
        "*/api/v1/me",
        () =>
          new HttpResponse("<html>Bad gateway</html>", {
            status: 502,
            statusText: "Bad Gateway",
            headers: { "Content-Type": "text/html", "X-Request-Id": "req-proxy" },
          }),
      ),
    );
    await expect(client().GET("/api/v1/me")).rejects.toMatchObject({
      status: 502,
      code: "http_502",
      message: "Bad Gateway",
      requestId: "req-proxy",
      isClientError: false,
    });
  });

  it("reports network failures as network_error", async () => {
    server.use(http.get("*/api/v1/me", () => HttpResponse.error()));
    await expect(client().GET("/api/v1/me")).rejects.toMatchObject({
      status: 0,
      code: "network_error",
    });
  });

  it("announces an expired session", async () => {
    const listener = vi.fn();
    const stop = onAuthExpired(listener);
    server.use(
      http.get("*/api/v1/me", () =>
        HttpResponse.json(
          { error: { code: "session_expired", message: "Your session has expired." } },
          { status: 401 },
        ),
      ),
    );
    await expect(client().GET("/api/v1/me")).rejects.toMatchObject({ code: "session_expired" });
    expect(listener).toHaveBeenCalledOnce();
    stop();
  });

  it("does not redirect anonymous visitors (401 unauthenticated)", async () => {
    const listener = vi.fn();
    const stop = onAuthExpired(listener);
    server.use(
      http.get("*/api/v1/me", () =>
        HttpResponse.json(
          { error: { code: "unauthenticated", message: "Sign in to continue." } },
          { status: 401 },
        ),
      ),
    );
    await expect(client().GET("/api/v1/me")).rejects.toMatchObject({ status: 401 });
    expect(listener).not.toHaveBeenCalled();
    stop();
  });

  it("does not announce other failures as an expired session", async () => {
    const listener = vi.fn();
    const stop = onAuthExpired(listener);
    server.use(http.get("*/api/v1/me", () => HttpResponse.json({}, { status: 403 })));
    await expect(client().GET("/api/v1/me")).rejects.toBeInstanceOf(ApiError);
    expect(listener).not.toHaveBeenCalled();
    stop();
  });
});

describe("readCsrfCookie", () => {
  it.each([
    ["session=abc; csrf=tok%3D1", "tok=1"],
    ["__Host-session=abc; __Host-csrf=secure-token", "secure-token"],
    ["other=1", null],
    ["", null],
    ["xcsrf=nope", null],
    ["csrf=stale; __Host-csrf=fresh", "fresh"],
    ["csrf=first; csrf=second", "first"],
  ])("%s → %s", (header, expected) => {
    expect(readCsrfCookie(header)).toBe(expected);
  });
});
