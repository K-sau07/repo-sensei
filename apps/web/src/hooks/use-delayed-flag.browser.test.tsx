import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "vitest-browser-react";

import { useDelayedFlag } from "./use-delayed-flag";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
});

afterEach(() => {
  vi.useRealTimers();
});

async function setup() {
  return renderHook((props?: { active: boolean }) => useDelayedFlag(props?.active ?? false), {
    initialProps: { active: true },
  });
}

/** Moves the fake clock and lets React commit the resulting state updates. */
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("useDelayedFlag", () => {
  it("never shows for work that finishes within 200ms", async () => {
    const { result, rerender } = await setup();
    await advance(150);
    await rerender({ active: false });
    await advance(1000);
    expect(result.current).toBe(false);
  });

  it("shows after 200ms and stays at least 400ms", async () => {
    const { result, rerender } = await setup();
    await advance(199);
    expect(result.current).toBe(false);
    await advance(1);
    expect(result.current).toBe(true);

    await advance(50); // the work finishes 50ms after the spinner appeared
    await rerender({ active: false });
    await advance(349);
    expect(result.current).toBe(true);
    await advance(1);
    expect(result.current).toBe(false);
  });

  it("hides at once when the spinner was already visible long enough", async () => {
    const { result, rerender } = await setup();
    await advance(1000);
    expect(result.current).toBe(true);
    await rerender({ active: false });
    await advance(0);
    expect(result.current).toBe(false);
  });

  it("stays visible when work restarts during the minimum time", async () => {
    const { result, rerender } = await setup();
    await advance(250);
    await rerender({ active: false });
    await advance(100);
    await rerender({ active: true });
    await advance(1000);
    expect(result.current).toBe(true);
  });
});
