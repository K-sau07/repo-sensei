import { describe, expect, it } from "vitest";

import type { ConstellationPage } from "@/lib/api/types";
import { EMPTY_SKY, type Sky } from "@/lib/constellation";
import { SKY } from "@/mocks/index-run";

import { readPages } from "./use-constellation";

const all = SKY.points;

function pageAt(
  since: number,
  size: number,
  fields: Partial<ConstellationPage> = {},
): ConstellationPage {
  const points = all.slice(since, since + size);
  return {
    layout: "directory",
    status: "complete",
    commit_sha: "a".repeat(40),
    generation: "g1",
    total: all.length,
    available: all.length,
    since,
    next: since + points.length,
    dirs: since === 0 ? SKY.dirs : [],
    points,
    ...fields,
  };
}

describe("reading a constellation's pages", () => {
  it("reads from its cursor to the end, showing each page as it comes", async () => {
    const asked: number[] = [];
    const shown: number[] = [];
    const sky = await readPages(
      EMPTY_SKY,
      (since) => {
        asked.push(since);
        return Promise.resolve(pageAt(since, 100));
      },
      (current: Sky) => shown.push(current.points.length),
    );
    expect(sky.points).toEqual(all);
    const pages = Math.ceil(all.length / 100);
    expect(asked).toEqual(Array.from({ length: pages }, (_, i) => i * 100));
    expect(shown).toEqual(asked.map((since) => Math.min(since + 100, all.length)));
  });

  it("starts over when discovery started over (another generation)", async () => {
    let calls = 0;
    const sky = await readPages(
      EMPTY_SKY,
      (since) => {
        calls += 1;
        // The first page is of g1; the next answer is already of g2, from where it was.
        if (calls === 1) return Promise.resolve(pageAt(since, 50, { status: "streaming" }));
        return Promise.resolve(pageAt(since, all.length, { generation: "g2" }));
      },
      () => undefined,
    );
    expect(sky.generation).toBe("g2");
    expect(sky.points).toEqual(all);
  });

  it("reads nothing more once the sky is complete", async () => {
    const done: Sky = { ...EMPTY_SKY, status: "complete" };
    const sky = await readPages(
      done,
      () => Promise.reject(new Error("asked")),
      () => undefined,
    );
    expect(sky).toBe(done);
  });
});
