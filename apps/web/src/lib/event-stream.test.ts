import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { followStream, MAX_RETRIES, RETRY_MS } from "./event-stream";

/** Enough of EventSource to drive from a test. */
class FakeSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static opened: FakeSource[] = [];
  readyState = FakeSource.CONNECTING;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, (message: MessageEvent<string>) => void>();
  constructor(readonly url: string) {
    FakeSource.opened.push(this);
  }
  addEventListener(type: string, listener: (message: MessageEvent<string>) => void) {
    this.listeners.set(type, listener);
  }
  close() {
    this.readyState = FakeSource.CLOSED;
  }
  /** The browser gave up on it (an error response to a reconnect). */
  giveUp() {
    this.readyState = FakeSource.CLOSED;
    this.onerror?.();
  }
}

describe("following a stream", () => {
  beforeEach(() => {
    FakeSource.opened = [];
    vi.useFakeTimers();
    vi.stubGlobal("EventSource", FakeSource);
    vi.stubEnv("NEXT_PUBLIC_MOCKS", "");
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("reopens after the browser gives up, resuming after the last event", () => {
    let last: string | null = null;
    const live: boolean[] = [];
    const close = followStream("/events", {
      types: ["done"],
      onEvent: () => undefined,
      onLive: (value) => live.push(value),
      lastId: () => last,
    });
    const [first] = FakeSource.opened;
    expect(first?.url).toBe("/events");
    first?.onopen?.();
    last = "5-0";
    first?.giveUp();
    expect(FakeSource.opened).toHaveLength(1); // not straight away
    vi.advanceTimersByTime(RETRY_MS[0] ?? 0);
    expect(FakeSource.opened.at(-1)?.url).toBe("/events?last_event_id=5-0");
    expect(live).toEqual([true, false]);
    close();
  });

  it("leaves reconnecting to the browser while it is still trying", () => {
    followStream("/events", {
      types: [],
      onEvent: () => undefined,
      onLive: () => undefined,
      lastId: () => null,
    });
    const [first] = FakeSource.opened;
    first?.onerror?.(); // readyState CONNECTING: the browser retries itself
    vi.advanceTimersByTime(60_000);
    expect(FakeSource.opened).toHaveLength(1);
  });

  it("doesn't reopen once the run ended, or once closed", () => {
    let finished = false;
    const close = followStream("/events", {
      types: [],
      onEvent: () => undefined,
      onLive: () => undefined,
      lastId: () => null,
      finished: () => finished,
    });
    finished = true;
    FakeSource.opened[0]?.giveUp();
    vi.advanceTimersByTime(60_000);
    expect(FakeSource.opened).toHaveLength(1);
    close();
  });

  it("stops after too many failures in a row", () => {
    followStream("/events?x=1", {
      types: [],
      onEvent: () => undefined,
      onLive: () => undefined,
      lastId: () => "1-0",
    });
    for (let attempt = 0; attempt <= MAX_RETRIES + 2; attempt += 1) {
      FakeSource.opened.at(-1)?.giveUp();
      vi.advanceTimersByTime(30_000);
    }
    expect(FakeSource.opened).toHaveLength(MAX_RETRIES + 1);
    expect(FakeSource.opened.at(-1)?.url).toBe("/events?x=1&last_event_id=1-0");
  });
});
