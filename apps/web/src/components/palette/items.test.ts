import { describe, expect, it } from "vitest";

import type { Repo } from "@/lib/api/types";
import type { Feature } from "@/lib/indexing";
import type { HistoryEntry } from "@/lib/palette/history";

import {
  askHref,
  askItem,
  askTarget,
  historyItems,
  isFullCommit,
  linesOf,
  pageItems,
  permalinkFor,
  searchItem,
  symbolItem,
  type RepoContext,
} from "./items";

const SHA = "b1125d8839482cb7bbae603ddc6d0081bf87532e";

const repo = (unlocked: Feature[], admin = false): RepoContext => ({
  slug: "api",
  indexedSha: SHA,
  version: SHA,
  indexing: false,
  admin,
  featuresReady: true,
  unlocked: (feature) => unlocked.includes(feature),
});

const entry = (fields: Partial<HistoryEntry>): HistoryEntry => ({
  id: "x",
  kind: "page",
  title: "x",
  href: "/acme",
  org: "acme",
  at: 1,
  count: 1,
  ...fields,
});

describe("pages", () => {
  const titles = (items: { title: string }[]) => items.map((item) => item.title);

  it("offers the repository's unlocked tabs, and admin-only ones to admins", () => {
    const member = titles(pageItems("acme", repo(["code_browser"]), false));
    expect(member).toContain("Code");
    expect(member).not.toContain("Ask"); // not unlocked yet
    expect(member).not.toContain("Settings"); // admins only
    expect(member).not.toContain("Overview"); // the repository item is that
    expect(titles(pageItems("acme", repo(["code_browser", "ask"], true), true))).toEqual(
      expect.arrayContaining(["Ask", "Settings", "Connect a repository"]),
    );
  });

  it("offers no tabs before the repository's index state has loaded", () => {
    const loading = { ...repo(["code_browser"]), featuresReady: false };
    expect(titles(pageItems("acme", loading, false))).not.toContain("Code");
  });

  it("are the org's sections outside a repository (settings is an action)", () => {
    const items = pageItems("acme", null, false);
    expect(titles(items)).toEqual([
      "Home",
      "Inbox",
      "Activity",
      "Repositories",
      "Insights",
      "Members",
    ]);
    expect(items.find((item) => item.title === "Inbox")?.href).toBe("/acme/inbox");
  });
});

describe("symbols", () => {
  it("open their lines in the code browser, at the ref being browsed", () => {
    const item = symbolItem("acme", "api", SHA, {
      symbol_key: "py backend/app/api/routes/login.py/login_access_token().",
      name: "login_access_token",
      qualified_name: "login_access_token",
      kind: "function",
      path: "backend/app/api/routes/login.py",
      start_line: 23,
      end_line: 42,
      signature: null,
      score: 1,
    });
    expect(item.href).toBe(`/acme/api/code/backend/app/api/routes/login.py?ref=${SHA}&L=23-42`);
    expect(item.subtitle).toBe("function · backend/app/api/routes/login.py:23");
    expect(linesOf({ start_line: 7, end_line: 7 })).toBe("7");
  });
});

describe("history", () => {
  it("shows this org's places that have a link", () => {
    const items = historyItems(
      [
        entry({ id: "a", title: "Inbox" }),
        entry({ id: "b", org: "other" }),
        { ...entry({ id: "c", kind: "action" }), href: undefined } as unknown as HistoryEntry,
      ],
      "acme",
    );
    expect(items.map((item) => item.id)).toEqual(["a"]);
  });
});

describe("ask", () => {
  const repos = [{ slug: "api" }, { slug: "web" }] as Repo[];

  it("goes to the current repository, else the last one visited, else the first", () => {
    expect(askTarget("web", [], repos, "acme")).toBe("web");
    const visited = [entry({ id: "p" }), entry({ id: "q", repo: "web" })];
    expect(askTarget(null, visited, repos, "acme")).toBe("web");
    // One that is gone (or of another org) doesn't count.
    expect(askTarget(null, [entry({ repo: "gone" })], repos, "acme")).toBe("api");
    expect(askTarget(null, [entry({ org: "other", repo: "web" })], repos, "acme")).toBe("api");
  });

  it("waits for the repository list, and never goes to one that isn't in it", () => {
    const visited = [entry({ repo: "web" })];
    expect(askTarget(null, visited, undefined, "acme")).toBeUndefined();
    expect(askTarget(null, visited, [], "acme")).toBeNull();
    expect(askTarget(null, [entry({ repo: "gone" })], [], "acme")).toBeNull();
    // In a repository, the URL says where (its page checks it exists).
    expect(askTarget("api", [], undefined, "acme")).toBe("api");
  });

  it("carries the query, encoded", () => {
    expect(askHref("acme", "api", " where is a&b? ")).toBe("/acme/api/ask?q=where+is+a%26b%3F");
    expect(askHref("acme", "api", "")).toBe("/acme/api/ask");
  });

  it("is shown but can't be picked without a repository, or while the list loads", () => {
    expect(askItem("acme", null, "hi").disabled?.reason).toMatch(/Connect a repository/);
    expect(askItem("acme", undefined, "hi").disabled?.reason).toBe("Checking repositories…");
    expect(askItem("acme", undefined, "hi").href).toBeUndefined();
    expect(askItem("acme", "api", "hi")).toMatchObject({
      title: "Ask “hi”",
      subtitle: "about api",
    });
  });
});

describe("search", () => {
  it("opens the repository's search page with what's typed, once something is", () => {
    expect(searchItem("acme", "api", "  password hash ")).toMatchObject({
      group: "ask",
      title: "Search “password hash”",
      subtitle: "in api",
      href: "/acme/api/search?q=password+hash",
    });
    expect(searchItem("acme", "api", "   ")).toBeNull();
  });
});

describe("permalinks", () => {
  const RENDERED = "c".repeat(40);
  const SHA256 = "d".repeat(64);
  const page = `https://sensei.test/acme/api/code/backend/app/main.py?L=10-12`;
  const pin = (href: string, rendered: string | null, indexed: string | null = SHA) =>
    permalinkFor(href, true, { rendered, indexed });

  it("pin a code page to the commit it showed, keeping the lines", () => {
    expect(pin(page, RENDERED)).toBe(`${page}&ref=${RENDERED}`);
  });

  it("replace a branch with the commit shown (its newest snapshot, maybe not the last index)", () => {
    expect(pin(`${page}&ref=main`, RENDERED)).toBe(`${page}&ref=${RENDERED}`);
  });

  it("replace a commit prefix with the whole commit it showed", () => {
    expect(pin(`${page}&ref=b1125d8`, RENDERED)).toBe(`${page}&ref=${RENDERED}`);
  });

  it("keep a whole commit already in the URL: SHA-1 or SHA-256", () => {
    const sha1 = "a".repeat(40);
    expect(pin(`${page}&ref=${sha1}`, RENDERED)).toBe(`${page}&ref=${sha1}`);
    expect(pin(`${page}&ref=${SHA256}`, RENDERED)).toBe(`${page}&ref=${SHA256}`);
    expect(pin(page, SHA256)).toBe(`${page}&ref=${SHA256}`);
    expect(isFullCommit(SHA256)).toBe(true);
    expect(isFullCommit("a".repeat(41))).toBe(false);
  });

  it("use the last index only for a page that showed nothing and has no ref", () => {
    expect(pin(page, null)).toBe(`${page}&ref=${SHA}`);
    // A branch with nothing shown yet stays as it is rather than pinned to a guess.
    expect(pin(`${page}&ref=main`, null)).toBe(`${page}&ref=main`);
    expect(pin(page, null, null)).toBe(page);
  });

  it("leave other pages as they are", () => {
    expect(
      permalinkFor("https://sensei.test/acme/api/ask", false, { rendered: SHA, indexed: SHA }),
    ).toBe("https://sensei.test/acme/api/ask");
  });
});
