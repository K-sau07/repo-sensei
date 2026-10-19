import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/errors";

const post = vi.fn();
const toastError = vi.fn();
vi.mock("@/lib/api/client", () => ({ api: { POST: post } }));
vi.mock("sonner", () => ({ toast: { error: toastError } }));

const { signOut } = await import("./sign-out");

const assign = vi.fn();
beforeEach(() => {
  vi.stubGlobal("window", { location: { assign } });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

const apiError = (status: number) =>
  new ApiError({ status, code: "x", message: "x", requestId: null, details: null });

describe("signOut", () => {
  it("goes home after the session ends", async () => {
    post.mockResolvedValue({});
    await expect(signOut()).resolves.toBe(true);
    expect(assign).toHaveBeenCalledWith("/");
  });

  it("can come back to a page afterwards", async () => {
    post.mockResolvedValue({});
    await signOut("/invite/abc");
    expect(assign).toHaveBeenCalledWith("/invite/abc");
  });

  it("treats an already-ended session as signed out", async () => {
    post.mockRejectedValue(apiError(401));
    await expect(signOut()).resolves.toBe(true);
    expect(assign).toHaveBeenCalledWith("/");
    expect(toastError).not.toHaveBeenCalled();
  });

  it.each([
    ["a refused request", apiError(403)],
    ["a network failure", new TypeError("Failed to fetch")],
  ])("stays signed in and says so after %s", async (_, error) => {
    post.mockRejectedValue(error);
    await expect(signOut()).resolves.toBe(false);
    expect(assign).not.toHaveBeenCalled();
    expect(toastError).toHaveBeenCalledOnce();
  });
});
