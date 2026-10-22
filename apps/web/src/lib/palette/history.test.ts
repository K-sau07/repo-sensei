import { beforeEach, describe, expect, it } from "vitest";

import {
  clearHistory,
  HISTORY_CAP,
  historyKey,
  isSafeHref,
  readHistory,
  recordHistory,
  REPEAT_WINDOW_MS,
  type HistoryInput,
} from "./history";

class MemoryStorage {
  values = new Map<string, string>();
  failWrites = false;
  get length() {
    return this.values.size;
  }
  key(index: number) {
    return [...this.values.keys()][index] ?? null;
  }
  getItem(key: string) {
    return this.values.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    if (this.failWrites) throw new Error("QuotaExceededError");
    this.values.set(key, value);
  }
  removeItem(key: string) {
    this.values.delete(key);
  }
}

const USER = "0199f000-0000-7000-8000-000000000001";
const OTHER = "0199f000-0000-7000-8000-000000000002";
let storage: MemoryStorage;
beforeEach(() => {
  storage = new MemoryStorage();
});

const place = (n: number): HistoryInput => ({
  id: `page:/acme/p${String(n)}`,
  kind: "page",
  title: `Page ${String(n)}`,
  href: `/acme/p${String(n)}`,
  org: "acme",
});

const record = (input: HistoryInput, at: number, user: string = USER) =>
  recordHistory(user, input, at, storage);
const read = (user: string = USER) => readHistory(user, storage);

describe("palette history", () => {
  it("keeps the most recent first and counts repeat uses", () => {
    record(place(1), 1_000_000);
    record(place(2), 2_000_000);
    record(place(1), 3_000_000);
    const entries = read();
    expect(entries.map((entry) => entry.id)).toEqual([place(1).id, place(2).id]);
    expect(entries[0]?.count).toBe(2);
    expect(entries[0]?.at).toBe(3_000_000);
  });

  it("doesn't count a use again within a few seconds (a pick followed by its visit)", () => {
    record(place(1), 1_000_000);
    record(place(1), 1_000_000 + REPEAT_WINDOW_MS - 1);
    expect(read()[0]?.count).toBe(1);
  });

  it(`is capped at ${String(HISTORY_CAP)}, dropping the oldest`, () => {
    for (let n = 0; n < HISTORY_CAP + 10; n += 1) record(place(n), 1_000_000 + n);
    const entries = read();
    expect(entries).toHaveLength(HISTORY_CAP);
    expect(entries[0]?.id).toBe(place(HISTORY_CAP + 9).id);
    expect(entries.at(-1)?.id).toBe(place(10).id);
  });

  it.each([
    ["not JSON", "{oops"],
    ["not a list", JSON.stringify({ id: "x" })],
    ["null", "null"],
    ["a number", "42"],
  ])("reads %s as empty, and recording starts over", (_label, raw) => {
    storage.values.set(historyKey(USER), raw);
    expect(read()).toEqual([]);
    record(place(1), 1_000_000);
    expect(read().map((entry) => entry.id)).toEqual([place(1).id]);
  });

  it("drops malformed entries and keeps the good ones", () => {
    const good = { ...place(1), at: 1_000_000, count: 3 };
    storage.values.set(
      historyKey(USER),
      JSON.stringify([
        good,
        null,
        "page:/acme",
        { ...good, id: "" },
        { ...good, id: "a", kind: "spaceship" },
        { ...good, id: "b", at: "yesterday" },
        { ...good, id: "c", at: Number.MAX_VALUE * 2 },
        { ...good, id: "d", title: "x".repeat(10_000) },
        { ...good, id: "e", org: 7 },
        // Links that would leave the site, or run script, are never trusted.
        { ...good, id: "f", href: "javascript:alert(1)" },
        { ...good, id: "g", href: "//evil.example/acme" },
        { ...good, id: "h", href: "https://evil.example" },
        { ...good, id: "i", href: "/\\evil.example" },
        { ...good, id: "k", href: "/\t/evil.example" },
        { ...good, id: "l", href: "/\n/evil.example" },
        { ...good, id: "j", count: -4 },
      ]),
    );
    const entries = read();
    expect(entries.map((entry) => entry.id).sort()).toEqual(["j", good.id]);
    // A bad count becomes 1; the entry stays.
    expect(entries.find((entry) => entry.id === "j")?.count).toBe(1);
  });

  it("keeps one entry per id (the most recent)", () => {
    const at = (time: number) => ({ ...place(1), at: time, count: 1 });
    storage.values.set(historyKey(USER), JSON.stringify([at(1), at(3), at(2)]));
    expect(read()).toHaveLength(1);
    expect(read()[0]?.at).toBe(3);
  });

  it("never throws when storage is full or missing", () => {
    storage.failWrites = true;
    expect(() => record(place(1), 1_000_000)).not.toThrow();
    expect(readHistory(USER, null)).toEqual([]);
    expect(recordHistory(USER, place(1), 1_000_000, null)).toEqual([]);
  });

  it("refuses to record an entry that isn't one", () => {
    record({ ...place(1), href: "javascript:void 0" }, 1_000_000);
    record({ ...place(2), href: "/\t/evil.example" }, 1_000_000);
    expect(read()).toEqual([]);
  });
});

describe("whose history", () => {
  it("is each user's own: another user on the same browser never sees it", () => {
    record(place(1), 1_000_000, USER);
    record(place(2), 1_000_000, OTHER);
    expect(read(USER).map((entry) => entry.id)).toEqual([place(1).id]);
    expect(read(OTHER).map((entry) => entry.id)).toEqual([place(2).id]);
  });

  it("is nobody's while we don't know who is signed in", () => {
    expect(recordHistory(null, place(1), 1_000_000, storage)).toEqual([]);
    expect(storage.values.size).toBe(0);
    record(place(1), 1_000_000);
    expect(readHistory(null, storage)).toEqual([]);
  });

  it("is cleared for every user at once (sign-out, an ended session), and nothing else", () => {
    record(place(1), 1_000_000, USER);
    record(place(2), 1_000_000, OTHER);
    storage.values.set("sensei.palette.history.v1", "[]"); // from before history was per user
    storage.values.set("sensei.shell.v1", "{}");
    clearHistory(storage);
    expect([...storage.values.keys()]).toEqual(["sensei.shell.v1"]);
  });
});

describe("isSafeHref", () => {
  it.each(["/acme", "/acme/api/code/backend/app/main.py?L=1-2", "/acme/api/ask?q=a%20b"])(
    "accepts the path %s",
    (href) => {
      expect(isSafeHref(href)).toBe(true);
    },
  );

  it.each([
    "//evil.example",
    "/\t/evil.example",
    "/\n/evil.example",
    "/\r/evil.example",
    "/ /evil.example",
    "/ /evil.example",
    "/\\evil.example",
    "/acme\u0000",
    "/acme\u007f",
    "javascript:alert(1)",
    "https://evil.example/acme",
    "acme",
    "",
  ])("refuses %j", (href) => {
    expect(isSafeHref(href)).toBe(false);
  });
});
