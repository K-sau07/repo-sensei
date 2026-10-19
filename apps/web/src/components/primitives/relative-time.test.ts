import { describe, expect, it } from "vitest";

import { formatEta } from "@/components/feedback/locked-state";

import { formatRelative } from "./relative-time";

const NOW = new Date("2026-10-18T12:00:00Z");
const ago = (seconds: number) => new Date(NOW.getTime() - seconds * 1000);

describe("formatRelative", () => {
  it.each([
    [10, "just now"],
    [-30, "just now"],
    [90, "2 minutes ago"],
    [59 * 60, "59 minutes ago"],
    [2 * 3600, "2 hours ago"],
    [26 * 3600, "yesterday"],
    [3 * 24 * 3600, "3 days ago"],
    [14 * 24 * 3600, "2 weeks ago"],
    [-2 * 24 * 3600, "in 2 days"],
    [400 * 24 * 3600, "last year"],
    [59.6 * 60, "1 hour ago"], // rounds up into the next unit, never "60 minutes ago"
    [23.6 * 3600, "yesterday"],
    [6.6 * 24 * 3600, "last week"],
    [45 * 24 * 3600, "2 months ago"],
  ])("%is ago is %s", (seconds, expected) => {
    expect(formatRelative(ago(seconds), NOW)).toBe(expected);
  });
});

describe("formatEta", () => {
  it.each([
    [20, "under a minute"],
    [180, "3m"],
    [3600, "1h"],
    [5400, "1h 30m"],
  ])("%is is %s", (seconds, expected) => {
    expect(formatEta(seconds)).toBe(expected);
  });
});
