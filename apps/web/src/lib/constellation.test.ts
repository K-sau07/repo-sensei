import { describe, expect, it } from "vitest";

import type { ConstellationPage } from "@/lib/api/types";
import { RUN_DETAILS, runEvents, SKY } from "@/mocks/index-run";

import {
  addPage,
  clusters,
  codeSection,
  EMPTY_SKY,
  isLowPower,
  isSoftwareRenderer,
  nearest,
  project,
  reach,
  stagger,
  syntheticSky,
} from "./constellation";

const page = (fields: Partial<ConstellationPage>): ConstellationPage => ({
  layout: "directory",
  status: "streaming",
  commit_sha: "a".repeat(40),
  generation: "g1",
  total: SKY.total,
  available: SKY.total ?? 0,
  since: 0,
  next: 0,
  dirs: [],
  points: [],
  ...fields,
});

describe("gathering pages", () => {
  it("appends pages in cursor order and keeps the directories of the first", () => {
    const first = page({ since: 0, next: 100, dirs: SKY.dirs, points: SKY.points.slice(0, 100) });
    let sky = addPage(EMPTY_SKY, first);
    expect(sky?.points).toHaveLength(100);
    expect(sky?.dirs).toHaveLength(SKY.dirs.length);
    sky = sky && addPage(sky, page({ since: 100, next: 245, points: SKY.points.slice(100) }));
    expect(sky?.points).toEqual(SKY.points);
    expect(sky?.dirs).toHaveLength(SKY.dirs.length);
    const done = sky && addPage(sky, page({ status: "complete", since: 245, next: 245 }));
    expect([done?.status, done?.points.length]).toEqual(["complete", 245]);
  });

  it("starts over for another generation, and refuses pages out of step", () => {
    const sky = addPage(EMPTY_SKY, page({ points: SKY.points.slice(0, 10), dirs: SKY.dirs }));
    if (!sky) throw new Error("no sky");
    // Discovery started over: a later page of the new generation can't be added.
    expect(
      addPage(sky, page({ generation: "g2", since: 10, points: SKY.points.slice(10, 20) })),
    ).toBeNull();
    // Its first page replaces everything.
    const fresh = addPage(sky, page({ generation: "g2", points: SKY.points.slice(0, 5) }));
    expect([fresh?.generation, fresh?.points.length]).toEqual(["g2", 5]);
    // A page that doesn't start where the picture ends.
    expect(addPage(sky, page({ since: 50, points: SKY.points.slice(50, 60) }))).toBeNull();
  });
});

describe("clusters", () => {
  it("counts each directory's files and names its top language", () => {
    const found = clusters({ ...EMPTY_SKY, dirs: SKY.dirs, points: SKY.points });
    const total = [...found.values()].reduce((sum, c) => sum + c.files, 0);
    expect(total).toBe(245);
    const backend = [...found.values()].find((c) => c.dir.path === "backend/app/api/routes");
    expect(backend?.topLanguage).toBe("python");
    // Directories with no files of their own aren't clusters.
    expect([...found.values()].every((c) => c.files > 0)).toBe(true);
  });

  it("breaks ties by name, whatever order the files came in", () => {
    const dir = { id: 0, path: "", parent: null, depth: 0, x: 0, y: 0, z: 0, files: 2 };
    const point = SKY.points[0];
    if (!point) throw new Error("no point");
    const a = { ...point, dir_cluster: 0, language: "typescript" };
    const b = { ...point, dir_cluster: 0, language: "python" };
    for (const points of [
      [a, b],
      [b, a],
    ]) {
      expect(clusters({ ...EMPTY_SKY, dirs: [dir], points }).get(0)?.topLanguage).toBe("python");
    }
  });
});

describe("geometry", () => {
  it("projects the centre to the middle and picks the nearest star", () => {
    expect(project(0, 0, 0).map((v) => Math.abs(v))).toEqual([0, 0, 0]);
    // Up in the layout is up on the screen (y grows downward).
    expect(project(0, 1, 0, { yaw: 0, pitch: 0 })).toEqual([0, -1, 0]);
    const screen = new Float32Array([10, 10, 50, 50, 52, 52]);
    expect(nearest(screen, 3, 51, 51.4, 5)).toBe(2);
    expect(nearest(screen, 3, 200, 200, 5)).toBe(-1);
  });

  it("knows how far the picture reaches", () => {
    const far = reach({ ...EMPTY_SKY, dirs: SKY.dirs, points: SKY.points });
    expect(far).toBeGreaterThan(0.5);
    expect(far).toBeLessThan(1);
    expect(reach(EMPTY_SKY)).toBe(0.25);
  });

  it("spreads a batch's arrival over at most the spread", () => {
    const delays = stagger(500);
    expect(delays[0]).toBe(0);
    expect(delays.at(-1)).toBe(1200);
    expect(stagger(10).at(-1)).toBe(40);
    expect(stagger(1)).toEqual([0]);
  });
});

describe("devices", () => {
  it("treats few cores or Save-Data as low power", () => {
    expect(isLowPower({ hardwareConcurrency: 2 })).toBe(true);
    expect(isLowPower({ hardwareConcurrency: 8, connection: { saveData: true } })).toBe(true);
    expect(isLowPower({ hardwareConcurrency: 8 })).toBe(false);
    expect(isLowPower({})).toBe(false);
  });

  it("recognizes software WebGL renderers", () => {
    expect(
      isSoftwareRenderer("ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (LLVM 10.0.0)))"),
    ).toBe(true);
    expect(isSoftwareRenderer("llvmpipe (LLVM 15.0.7, 256 bits)")).toBe(true);
    expect(isSoftwareRenderer("ANGLE (Microsoft, Microsoft Basic Render Driver, D3D11)")).toBe(
      true,
    );
    expect(isSoftwareRenderer("ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Pro)")).toBe(false);
    expect(isSoftwareRenderer("ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11)")).toBe(false);
    expect(isSoftwareRenderer(null)).toBe(false);
  });
});

describe("links and made-up skies", () => {
  it("encodes every path segment for the code browser", () => {
    expect(codeSection("src/a b/#c.ts")).toBe("code/src/a%20b/%23c.ts");
  });

  it("makes large, consistent synthetic skies", () => {
    const sky = syntheticSky(50_000, 3);
    expect(sky.points).toHaveLength(50_000);
    const files = sky.dirs.reduce((sum, d) => sum + d.files, 0);
    expect(files).toBe(50_000);
    expect(sky.points.every((p) => sky.dirs[p.dir_cluster]?.files)).toBe(true);
    expect(syntheticSky(100, 3)).toEqual(syntheticSky(100, 3));
  });
});

describe("the mock run's constellation", () => {
  it("is the recorded commit's 245 files, told once just before discovery's last batch", () => {
    expect(SKY.total).toBe((RUN_DETAILS.discover?.details as { files: number }).files);
    const events = runEvents();
    const told = events.filter((e) => e.type === "constellation");
    expect(told).toHaveLength(1);
    const at = events.findIndex((e) => e.type === "constellation");
    expect(events[at + 1]).toMatchObject({ type: "progress", stage: "discover", done: 245 });
    expect(told[0]).toMatchObject({ cursor: 245, total: 245, generation: SKY.generation });
    // A run that fails at the clone never gets that far.
    const failed = runEvents({ stage: "clone", code: "x", message: "y" });
    expect(failed.some((e) => e.type === "constellation")).toBe(false);
  });
});
