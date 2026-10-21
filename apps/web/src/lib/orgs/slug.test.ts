import { readdirSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { PUBLIC_PREFIXES } from "@/lib/security/auth-paths";

import { RESERVED_SLUGS, slugify, slugProblem } from "./slug";

describe("slugify", () => {
  it.each([
    ["Acme Inc.", "acme-inc"],
    ["  Beta   Labs  ", "beta-labs"],
    ["Café Ünïcode", "cafe-unicode"],
    ["--Weird__Name--", "weird-name"],
    ["a".repeat(60), "a".repeat(39)],
    ["ab-".repeat(13), "ab-".repeat(13).slice(0, 38)],
  ])("%s → %s", (name, slug) => {
    expect(slugify(name)).toBe(slug);
  });
});

describe("slugProblem", () => {
  it.each([
    ["acme", null],
    ["a1b", null],
    ["x".repeat(39), null],
    ["ab", "invalid_slug"],
    ["x".repeat(40), "invalid_slug"],
    ["-acme", "invalid_slug"],
    ["acme-", "invalid_slug"],
    ["ac--me", "invalid_slug"],
    ["Acme", "invalid_slug"],
    ["api", "slug_reserved"],
    ["settings", "slug_reserved"],
  ])("%s → %s", (slug, problem) => {
    expect(slugProblem(slug)).toBe(problem);
  });

  it("never suggests an invalid slug from a reasonable name", () => {
    for (const name of ["Acme Inc", "Big Co 2026", "Ünïcode Labs", "R&D Team"]) {
      expect(slugProblem(slugify(name))).toBeNull();
    }
  });
});

describe("reserved names", () => {
  // An org slug is the first path segment, so it must never shadow a page of our own.
  const appDir = path.join(__dirname, "..", "..", "app");
  const topLevelPages = readdirSync(appDir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && /^\(.+\)$/.test(entry.name))
    .flatMap((group) => readdirSync(path.join(appDir, group.name), { withFileTypes: true }))
    .filter((entry) => entry.isDirectory() && !entry.name.startsWith("["))
    .map((entry) => entry.name);

  it.each(PUBLIC_PREFIXES.map((prefix) => prefix.split("/")[1] ?? ""))(
    "public prefix /%s is reserved",
    (segment) => {
      expect(RESERVED_SLUGS.has(segment)).toBe(true);
    },
  );

  it("covers every top-level page", () => {
    expect(topLevelPages.length).toBeGreaterThan(3);
    expect(topLevelPages.filter((name) => !RESERVED_SLUGS.has(name))).toEqual([]);
  });
});
