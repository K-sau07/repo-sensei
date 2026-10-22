import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/errors";
import { historyKey } from "@/lib/palette/history";

const post = vi.fn();
const toastError = vi.fn();
vi.mock("@/lib/api/client", () => ({ api: { POST: post } }));
vi.mock("sonner", () => ({ toast: { error: toastError } }));

const { signOut } = await import("./sign-out");

const assign = vi.fn();
const removeItem = vi.fn();
const stored = [historyKey("user-1"), "sensei.shell.v1", historyKey("user-2")];
beforeEach(() => {
  vi.stubGlobal("window", {
    location: { assign },
    localStorage: { length: stored.length, key: (index: number) => stored[index], removeItem },
  });
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
    // The palette's recent places (every user's) don't outlive the session; nothing else goes.
    expect(removeItem.mock.calls).toEqual([[historyKey("user-1")], [historyKey("user-2")]]);
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
