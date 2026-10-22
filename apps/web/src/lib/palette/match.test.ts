import { describe, expect, it } from "vitest";

import { fuzzyMatch, matchItem, matchItems, mergeRanges, queryVariants } from "./match";

const highlighted = (text: string, ranges: readonly (readonly [number, number])[]) =>
  ranges.map(([start, end]) => text.slice(start, end));

describe("fuzzyMatch", () => {
  it("matches a subsequence, case-insensitively, and nothing else", () => {
    expect(fuzzyMatch("LGN", "login")).not.toBeNull();
    expect(fuzzyMatch("lgn", "LOGIN")).not.toBeNull();
    expect(fuzzyMatch("nl", "login")).toBeNull(); // out of order
    expect(fuzzyMatch("loginx", "login")).toBeNull();
    expect(fuzzyMatch("", "login")).toEqual({ score: 0, ranges: [] });
  });

  it("returns the matched characters as merged ranges", () => {
    const text = "login_access_token";
    const match = fuzzyMatch("logacc", text);
    expect(match?.ranges).toEqual([
      [0, 3],
      [6, 9],
    ]);
    expect(highlighted(text, match?.ranges ?? [])).toEqual(["log", "acc"]);
  });

  it("prefers word starts after separators (snake_case, paths, spaces)", () => {
    // Each piece starts a word; a run along a word beats jumping to the next one.
    const snake = fuzzyMatch("gcu", "get_current_user");
    expect(highlighted("get_current_user", snake?.ranges ?? [])).toEqual(["g", "cu"]);
    expect(fuzzyMatch("gcu", "get_current_user")?.score ?? 0).toBeGreaterThan(
      fuzzyMatch("gcu", "agecurrentuser")?.score ?? 0,
    );
    const path = fuzzyMatch("arl", "backend/app/api/routes/login.py");
    expect(highlighted("backend/app/api/routes/login.py", path?.ranges ?? [])).toEqual([
      "a",
      "r",
      "l",
    ]);
    // "api", not "app": both start a word, and the nearer one leaves the smaller gap.
    expect(path?.ranges).toEqual([
      [12, 13],
      [16, 17],
      [23, 24],
    ]);
  });

  it("prefers camelCase humps", () => {
    expect(fuzzyMatch("st", "SidebarTrigger")?.score ?? 0).toBeGreaterThan(
      fuzzyMatch("st", "Sidebarxtrigger")?.score ?? 0,
    );
    expect(fuzzyMatch("uam", "useAuthMutation")?.ranges).toEqual([
      [0, 1],
      [3, 4],
      [7, 8],
    ]);
  });

  it("scores the text itself 1, a prefix just below, word starts above mid-word runs", () => {
    expect(fuzzyMatch("inbox", "Inbox")?.score).toBe(1);
    const prefix = fuzzyMatch("inb", "Inbox")?.score ?? 0;
    const wordStart = fuzzyMatch("box", "In box")?.score ?? 0;
    const midWord = fuzzyMatch("box", "Inbox")?.score ?? 0;
    expect(prefix).toBeLessThan(1);
    expect(prefix).toBeGreaterThan(wordStart);
    expect(wordStart).toBeGreaterThan(midWord);
  });

  it("ranks contiguous matches above the same letters scattered", () => {
    const contiguous = fuzzyMatch("theme", "Switch to dark theme")?.score ?? 0;
    const scattered = fuzzyMatch("theme", "the_mock_e")?.score ?? 0;
    expect(contiguous).toBeGreaterThan(scattered);
  });

  it("ranks the shorter of two equal matches higher", () => {
    expect(fuzzyMatch("in", "Inbox")?.score ?? 0).toBeGreaterThan(
      fuzzyMatch("in", "Insights")?.score ?? 0,
    );
  });

  it("keeps scores between 0 and 1", () => {
    for (const [query, text] of [
      ["a", "a"],
      ["ab", "a_b"],
      ["abc", "xaxbxcx"],
      ["login", "backend/app/api/routes/login.py"],
    ] as const) {
      const score = fuzzyMatch(query, text)?.score ?? -1;
      expect(score).toBeGreaterThan(0);
      expect(score).toBeLessThanOrEqual(1);
    }
  });

  it("is deterministic", () => {
    const text = "frontend/src/components/ui/sidebar.tsx";
    expect(fuzzyMatch("sbar", text)).toEqual(fuzzyMatch("sbar", text));
  });
});

describe("Unicode", () => {
  it("matches characters whose lowercase is longer (İ → i̇)", () => {
    const match = fuzzyMatch("ist", "İstanbul");
    expect(match?.ranges).toEqual([[0, 3]]);
    expect(fuzzyMatch("istanbul", "İstanbul")?.score).toBe(1);
    expect(fuzzyMatch("İST", "istanbul")).not.toBeNull();
  });

  it("matches across NFC and NFD, highlighting whole letters with their accents", () => {
    const decomposed = "cafe\u0301 menu"; // "café", e + combining acute
    const composed = "caf\u00e9 menu";
    expect(fuzzyMatch("café", decomposed)?.ranges).toEqual([[0, 5]]);
    expect(fuzzyMatch("cafe\u0301", composed)?.ranges).toEqual([[0, 4]]);
    // A match on the accented letter takes its mark with it, in the original string.
    const [range] = fuzzyMatch("é", decomposed)?.ranges ?? [];
    expect(range).toEqual([3, 5]);
    expect(decomposed.slice(range?.[0], range?.[1])).toBe("e\u0301");
  });

  it("keeps ranges on the original text for letters outside the BMP", () => {
    const text = "𝒜lpha beta";
    const [range] = fuzzyMatch("beta", text)?.ranges ?? [];
    expect(text.slice(range?.[0], range?.[1])).toBe("beta");
  });
});

describe("mergeRanges", () => {
  it("sorts, merges overlapping and touching ranges", () => {
    expect(
      mergeRanges([
        [5, 7],
        [0, 2],
        [2, 3],
        [6, 9],
      ]),
    ).toEqual([
      [0, 3],
      [5, 9],
    ]);
  });
});

describe("synonyms", () => {
  it("reads a query through each synonym, literal first", () => {
    expect(queryVariants("remove")).toEqual([
      { text: "remove", synonym: false },
      { text: "delete", synonym: true },
    ]);
    expect(queryVariants("Log out").map((variant) => variant.text)).toEqual([
      "log out",
      "sign out",
      "logout",
    ]);
  });

  it("reads a word being typed as a synonym's start once it has five characters", () => {
    expect(queryVariants("refre").map((variant) => variant.text)).toContain("reindex");
    expect(queryVariants("prefer").map((variant) => variant.text)).toContain("settings");
    expect(queryVariants("sign o").map((variant) => variant.text)).toContain("log out");
  });

  it.each(["log", "logo", "the", "lig", "set", "rem", "refr", "re"])(
    "doesn't read %s (a short start) through a synonym",
    (query) => {
      expect(queryVariants(query).map((variant) => variant.text)).toEqual([query]);
    },
  );

  it("still reads whole short words through their synonyms", () => {
    expect(queryVariants("dark").map((variant) => variant.text)).toContain("theme");
  });

  it("says when a match came only through a synonym", () => {
    expect(matchItem("remove", { id: "a", title: "Delete" })?.synonym).toBe(true);
    expect(matchItem("remove", { id: "b", title: "Remove" })?.synonym).toBe(false);
    expect(matchItem("log", { id: "c", title: "Sign out" })).toBeNull();
  });

  it.each([
    ["delete", "remove", "Remove a member"],
    ["remove", "delete", "Delete the repository"],
    ["preferences", "settings", "Open settings"],
    ["settings", "preferences", "Preferences"],
    ["refresh", "reindex", "Reindex api"],
    ["rebuild", "reindex", "Reindex api"],
    ["reindex", "refresh", "Refresh the index"],
    ["log out", "sign out", "Sign out"],
    ["logout", "sign out", "Sign out"],
    ["sign out", "log out", "Log out"],
    ["dark", "theme", "Toggle theme"],
    ["theme", "dark", "Dark mode"],
    ["light", "theme", "Toggle theme"],
  ])("%s finds an item called %s", (query, _word, title) => {
    const match = matchItem(query, { id: "x", title });
    expect(match).not.toBeNull();
    expect(match?.title.length).toBeGreaterThan(0);
  });

  it("ranks a literal match above a synonym match", () => {
    const literal = matchItem("remove", { id: "a", title: "Remove" })?.score ?? 0;
    const synonym = matchItem("remove", { id: "b", title: "Delete" })?.score ?? 0;
    expect(literal).toBeGreaterThan(synonym);
    expect(synonym).toBeGreaterThan(0);
  });
});

describe("matchItem", () => {
  it("needs every word, in any field, and highlights title and subtitle separately", () => {
    const item = { id: "f", title: "login.py", subtitle: "api · backend/app/api/routes" };
    const match = matchItem("routes login", item);
    expect(match).not.toBeNull();
    expect(highlighted(item.title, match?.title ?? [])).toEqual(["login"]);
    expect(highlighted(item.subtitle, match?.subtitle ?? [])).toEqual(["routes"]);
    expect(matchItem("routes nothing", item)).toBeNull();
  });

  it("matches hidden keywords without highlighting anything", () => {
    const match = matchItem("hotkeys", {
      id: "s",
      title: "Show keyboard shortcuts",
      keywords: ["hotkeys"],
    });
    expect(match?.score).toBeGreaterThan(0.5);
    expect(match?.title).toEqual([]);
  });

  it("ranks a title match above the same match in the subtitle", () => {
    const title = matchItem("api", { id: "a", title: "api", subtitle: "x" })?.score ?? 0;
    const subtitle = matchItem("api", { id: "b", title: "x", subtitle: "api" })?.score ?? 0;
    expect(title).toBeGreaterThan(subtitle);
  });

  it("an empty query matches everything with score 0", () => {
    const items = [
      { id: "a", title: "A" },
      { id: "b", title: "B" },
    ];
    expect(matchItems("  ", items)).toEqual([
      { id: "a", score: 0, title: [], subtitle: [], synonym: false },
      { id: "b", score: 0, title: [], subtitle: [], synonym: false },
    ]);
  });
});
