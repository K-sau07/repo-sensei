import { describe, expect, it } from "vitest";

import type { HistoryEntry } from "./history";
import {
  CONTEXT_DIR,
  CONTEXT_FILE,
  CONTEXT_REPO,
  contextBoost,
  GROUP_CAP,
  normaliseServerScores,
  rank,
  RECENCY_HALF_LIFE_MS,
  RECENCY_MAX,
  recencyBoost,
  resultCount,
  selectionOrder,
  moreValue,
  toSections,
  type Candidate,
  type RankContext,
} from "./rank";
import type { PaletteItem } from "./types";

const NOW = Date.parse("2026-10-22T12:00:00Z");
const context: RankContext = {
  org: "acme",
  repo: "api",
  path: "backend/app/api/routes/login.py",
  pathIsFile: true,
  now: NOW,
};

function file(repo: string, path: string): PaletteItem {
  return {
    id: `code:acme/${repo}:${path}`,
    group: "go",
    kind: "file",
    title: path.slice(path.lastIndexOf("/") + 1),
    href: `/acme/${repo}/code/${path}`,
    scope: { org: "acme", repo, path },
  };
}

function candidate(item: PaletteItem, match: number, extra: Partial<Candidate> = {}): Candidate {
  return { item, match, title: [], subtitle: [], source: "local", order: 0, ...extra };
}

const entry = (id: string, ageMs: number, count = 1): HistoryEntry => ({
  id,
  kind: "file",
  title: id,
  href: "/acme",
  org: "acme",
  at: NOW - ageMs,
  count,
});

const ids = (rows: { item: PaletteItem }[]) => rows.map((row) => row.item.id);

describe("context boost", () => {
  it("lifts the current repository, more its directory, most the file itself", () => {
    const elsewhere = contextBoost(file("web", "backend/app/api/routes/items.py"), context);
    const repo = contextBoost(file("api", "frontend/src/main.tsx"), context);
    const dir = contextBoost(file("api", "backend/app/api/routes/items.py"), context);
    const same = contextBoost(file("api", "backend/app/api/routes/login.py"), context);
    expect(elsewhere).toBe(0);
    expect(repo).toBe(CONTEXT_REPO);
    expect(dir).toBeCloseTo(CONTEXT_REPO + CONTEXT_DIR);
    expect(same).toBeCloseTo(CONTEXT_REPO + CONTEXT_DIR + CONTEXT_FILE);
  });

  it("on a directory page, lifts what is in it, not its siblings", () => {
    const inDir = { ...context, path: "backend/app/api/routes", pathIsFile: false };
    expect(contextBoost(file("api", "backend/app/api/routes/items.py"), inDir)).toBeCloseTo(
      CONTEXT_REPO + CONTEXT_DIR,
    );
    // backend/app/api/deps.py sits beside routes/, not in it.
    expect(contextBoost(file("api", "backend/app/api/deps.py"), inDir)).toBe(CONTEXT_REPO);
    // And the directory itself isn't "the current file".
    const routes = file("api", "backend/app/api/routes");
    expect(contextBoost(routes, inDir)).toBe(CONTEXT_REPO);
  });

  it("at the code root, lifts the root's own files", () => {
    const atRoot = { ...context, path: "", pathIsFile: false };
    expect(contextBoost(file("api", "README.md"), atRoot)).toBeCloseTo(CONTEXT_REPO + CONTEXT_DIR);
    expect(contextBoost(file("api", "backend/app/main.py"), atRoot)).toBe(CONTEXT_REPO);
  });

  it("is nothing outside a repository or in another org", () => {
    const item = file("api", "backend/app/main.py");
    expect(contextBoost(item, { ...context, repo: null, path: null })).toBe(0);
    expect(contextBoost({ ...item, scope: { org: "other", repo: "api" } }, context)).toBe(0);
  });

  it("ranks a file in the current repository above an equal one elsewhere, even a recent one", () => {
    const here = file("api", "backend/app/api/routes/items.py");
    const there = file("web", "src/routes/items.py");
    const ranked = rank(
      [candidate(there, 0.9, { recent: true }), candidate(here, 0.9, { source: "server" })],
      { ...context, path: null },
      [entry(there.id, 60_000, 20)],
      "items",
    );
    expect(ids(ranked)).toEqual([here.id, there.id]);
  });
});

describe("recency boost", () => {
  it("is at most RECENCY_MAX and halves every half-life", () => {
    const fresh = recencyBoost(entry("a", 0, 10), NOW);
    const old = recencyBoost(entry("a", RECENCY_HALF_LIFE_MS, 10), NOW);
    expect(fresh).toBeCloseTo(RECENCY_MAX);
    expect(old).toBeCloseTo(RECENCY_MAX / 2);
    expect(recencyBoost(undefined, NOW)).toBe(0);
  });

  it("fades slower for things used often", () => {
    expect(recencyBoost(entry("a", 0, 5), NOW)).toBeGreaterThan(
      recencyBoost(entry("a", 0, 1), NOW),
    );
  });

  it("breaks a tie between equal matches", () => {
    const a = file("web", "a.py");
    const b = file("web", "b.py");
    const ranked = rank([candidate(a, 0.8), candidate(b, 0.8)], context, [entry(b.id, 1000)], "py");
    expect(ids(ranked)).toEqual([b.id, a.id]);
  });

  it("an empty query shows recent items first, most recent first, then provider order", () => {
    const page = { ...file("web", "z.py"), kind: "page" as const, id: "page:/acme/inbox" };
    const older = file("web", "older.py");
    const newer = file("web", "newer.py");
    const ranked = rank(
      [
        candidate(page, 0, { order: 0 }),
        candidate(older, 0, { recent: true, order: 1000 }),
        candidate(newer, 0, { recent: true, order: 1001 }),
      ],
      context,
      [entry(older.id, 50_000), entry(newer.id, 1000)],
      "",
    );
    expect(ids(ranked)).toEqual([newer.id, older.id, page.id]);
  });
});

describe("merging server results", () => {
  it("normalises scores to 0–1 without ever reordering them", () => {
    expect(normaliseServerScores([4.5, 3.6, 3.9, 0.9], 4.5)).toEqual([1, 0.8, 0.8, 0.2]);
    expect(normaliseServerScores([9, Number.NaN, -1], 4.5)).toEqual([1, 0, 0]);
  });

  it("keeps the server's order and merges it with local results by score", () => {
    const local: PaletteItem = {
      id: "page:/acme/inbox",
      group: "go",
      kind: "page",
      title: "Inbox",
      scope: { org: "acme" },
    };
    const first = file("api", "frontend/one.ts");
    const second = file("api", "frontend/two.ts");
    const [one, two] = normaliseServerScores([3.6, 2.7], 4.5);
    const ranked = rank(
      [
        candidate(second, two ?? 0, { source: "server", order: 2001 }),
        candidate(local, 0.7),
        candidate(first, one ?? 0, { source: "server", order: 2000 }),
      ],
      { ...context, repo: null, path: null },
      [],
      "o",
    );
    // 0.8, then the page's 0.7 between the server's 0.8 and 0.6.
    expect(ids(ranked)).toEqual([first.id, local.id, second.id]);
  });

  it("makes one row of an item found twice: the live one, marked recent, matched on its text", () => {
    const item = file("api", "backend/app/main.py");
    const stale = { ...item, title: "main.py (old title)" };
    const ranked = rank(
      [
        candidate(stale, 0.5, { recent: true, title: [[0, 1]] }),
        candidate(item, 0.9, { source: "server", title: [[0, 4]] }),
      ],
      context,
      [entry(item.id, 1000)],
      "main",
    );
    expect(ranked).toHaveLength(1);
    expect(ranked[0]?.item.title).toBe("main.py");
    expect(ranked[0]?.recent).toBe(true);
    expect(ranked[0]?.match).toBe(0.9);
    // History's entry matched its own (older) text better: the row still shows the live text,
    // so its score and highlight are the live ones.
    const better = rank(
      [
        candidate(stale, 0.95, { recent: true, title: [[0, 1]] }),
        candidate(item, 0.6, { source: "server", title: [[0, 4]] }),
      ],
      context,
      [entry(item.id, 1000)],
      "main",
    );
    expect([better[0]?.match, better[0]?.title]).toEqual([0.6, [[0, 4]]]);
  });
});

describe("determinism", () => {
  it("gives the identical order for identical input, whatever order it came in", () => {
    const items = ["b.py", "a.py", "c.py", "d.py"].map((path) => file("web", path));
    const input = items.map((item) => candidate(item, 0.5));
    const forward = ids(rank(input, context, [], "py"));
    const backward = ids(rank([...input].reverse(), context, [], "py"));
    expect(forward).toEqual(backward);
    expect(forward).toEqual(["a.py", "b.py", "c.py", "d.py"].map((p) => `code:acme/web:${p}`));
    expect(ids(rank(input, context, [], "py"))).toEqual(forward);
  });
});

describe("selection", () => {
  const signOut: PaletteItem = {
    id: "action:sign-out",
    group: "do",
    kind: "action",
    title: "Sign out",
    destructive: true,
  };
  const ask: PaletteItem = { id: "ask", group: "ask", kind: "ask", title: "Ask" };

  it("never auto-selects a destructive action only a synonym matched", () => {
    const ranked = rank(
      [candidate(signOut, 0.9, { synonym: true }), candidate(ask, 0)],
      context,
      [],
      "logout",
    );
    const order = selectionOrder(toSections(ranked));
    expect(order).toEqual([
      { value: signOut.id, auto: false },
      { value: "ask", auto: true },
    ]);
    // What the palette selects by itself: the first row it may.
    expect(order.find((row) => row.auto)?.value).toBe("ask");
  });

  it("does select it when what was typed names it", () => {
    const ranked = rank([candidate(signOut, 1), candidate(ask, 0)], context, [], "sign out");
    expect(selectionOrder(toSections(ranked))[0]).toEqual({ value: signOut.id, auto: true });
  });

  it("keeps a row literal if any source matched it literally", () => {
    const ranked = rank(
      [candidate(signOut, 0.6, { synonym: true }), candidate(signOut, 0.5)],
      context,
      [],
      "sign",
    );
    expect(ranked[0]?.synonym).toBeUndefined();
  });

  it("lists a capped group's more row after its rows, and skips disabled ones and notices", () => {
    const many = Array.from({ length: 8 }, (_, i) =>
      candidate(file("api", `f${String(i)}.py`), 0.5),
    );
    const off: PaletteItem = { ...signOut, id: "x", disabled: { reason: "no" } };
    const order = selectionOrder(
      toSections(rank([...many, candidate(off, 0.4)], context, [], "f")),
    );
    expect(order.map((row) => row.value).slice(-1)).toEqual([moreValue("go")]);
    expect(order.map((row) => row.value)).not.toContain("x");
  });
});

describe("result count", () => {
  it("counts every result, not just the rows the caps show (and never Ask or notices)", () => {
    const many = Array.from({ length: 9 }, (_, i) =>
      candidate(file("api", `f${String(i)}.py`), 0.5),
    );
    const notice: PaletteItem = {
      id: "notice:symbols",
      group: "go",
      kind: "page",
      title: "Couldn't search symbols",
      notice: true,
    };
    const ask: PaletteItem = { id: "ask", group: "ask", kind: "ask", title: "Ask" };
    const sections = toSections(
      rank([...many, candidate(notice, 0), candidate(ask, 0)], context, [], "f"),
    );
    expect(sections[0]?.rows.filter((row) => !row.item.notice)).toHaveLength(GROUP_CAP);
    expect(resultCount(sections)).toBe(9);
  });
});

describe("sections", () => {
  const action = (id: string): PaletteItem => ({ id, group: "do", kind: "action", title: id });

  it("caps each group and counts what it hides; expanded groups show up to 50", () => {
    const many = Array.from({ length: 9 }, (_, i) =>
      candidate(file("api", `f${String(i)}.py`), 0.5),
    );
    const ranked = rank(many, context, [], "f");
    const [go] = toSections(ranked);
    expect(go?.rows).toHaveLength(GROUP_CAP);
    expect(go?.hidden).toBe(9 - GROUP_CAP);
    const [open] = toSections(ranked, new Set(["go"]));
    expect(open?.rows).toHaveLength(9);
    expect(open?.hidden).toBe(0);
  });

  it("orders Go and Do by their best row; Ask is always last", () => {
    const ask: PaletteItem = { id: "ask", group: "ask", kind: "ask", title: "Ask" };
    const ranked = rank(
      [
        candidate(ask, 0.99),
        candidate(action("Sign out"), 0.95),
        candidate(file("web", "sign.py"), 0.4),
      ],
      context,
      [],
      "sign",
    );
    expect(toSections(ranked).map((section) => section.group)).toEqual(["do", "go", "ask"]);
  });

  it("keeps a provider's notice out of the cap, after the results", () => {
    const notice: PaletteItem = {
      id: "notice:symbols",
      group: "go",
      kind: "page",
      title: "Symbols appear after indexing",
      notice: true,
    };
    const many = Array.from({ length: 8 }, (_, i) =>
      candidate(file("api", `f${String(i)}.py`), 0.5),
    );
    const [go] = toSections(rank([candidate(notice, 1), ...many], context, [], "f"));
    expect(go?.rows).toHaveLength(GROUP_CAP + 1);
    expect(go?.rows.at(-1)?.item.id).toBe(notice.id);
  });
});
