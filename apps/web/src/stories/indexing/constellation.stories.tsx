import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { useState } from "react";
import { expect, fireEvent, waitFor, within } from "storybook/test";

import { Constellation, ConstellationView } from "@/components/indexing/constellation";
import { project, reach, syntheticSky, type Sky } from "@/lib/constellation";
import { API_REPO_ID, db, resetMockDb, saveDb } from "@/mocks/db";
import { SKY } from "@/mocks/index-run";
import { RUN_LENGTH_MS } from "@/mocks/jobs";

const nextjs = {
  appDirectory: true,
  navigation: {
    pathname: "/acme/api",
    segments: [
      ["org", "acme"],
      ["repo", "api"],
    ],
  },
};

/** The recorded commit's constellation (245 files), whole or as far as `found` points. */
function fixture(found = SKY.points.length): Sky {
  const complete = found >= SKY.points.length;
  return {
    generation: SKY.generation,
    status: complete ? "complete" : "streaming",
    commitSha: SKY.commit_sha,
    total: SKY.total,
    available: found,
    dirs: SKY.dirs,
    points: SKY.points.slice(0, found),
  };
}

/** Where on screen a star of the SVG view is, and the star furthest from its neighbours (so
 * the pointer finds that one and no other). */
function loneStar(svg: Element, sky: Sky) {
  const box = svg.getBoundingClientRect();
  const far = reach(sky);
  const scale = Math.min(box.width, box.height) / 2.2;
  const spots = sky.points.map((p) => {
    const [x, y] = project(p.x / far, p.y / far, p.z / far);
    return { x: box.left + box.width / 2 + x * scale, y: box.top + box.height / 2 + y * scale };
  });
  let best = 0;
  let index = 0;
  spots.forEach((a, i) => {
    let closest = Infinity;
    spots.forEach((b, j) => {
      if (i !== j) closest = Math.min(closest, Math.hypot(a.x - b.x, a.y - b.y));
    });
    if (closest > best) {
      best = closest;
      index = i;
    }
  });
  const spot = spots[index];
  const point = sky.points[index];
  if (!spot || !point) throw new Error("no stars");
  return { clientX: spot.x, clientY: spot.y, point };
}

const meta = {
  title: "Indexing/Constellation",
  component: ConstellationView,
  args: { sky: fixture(), org: "acme", repo: "api" },
  parameters: { nextjs },
} satisfies Meta<typeof ConstellationView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The last index of the fastapi template, drawn by the SVG fallback (the default in stories). */
export const Complete: Story = {
  play: async ({ canvas }) => {
    const figure = canvas.getByRole("figure", { name: /Constellation/ });
    await expect(figure).toBeVisible();
    const picture = canvas.getByRole("img", { name: /Constellation of the repository/ });
    await expect(picture).toHaveAccessibleName(
      /245 files in \d+ directories \(UI 99, API 10, Service 6, Data 10, Other 120\)/,
    );
    // Color is never the only signal: every layer is named in the legend, with its glyph.
    const legend = canvas.getByRole("list", { name: "Layers" });
    for (const name of ["UI", "API", "Service", "Data", "Other"]) {
      await expect(within(legend).getByText(name)).toBeVisible();
    }
  },
};

/** Hovering a star names its directory cluster; clicking it previews the file. */
export const HoverAndPreview: Story = {
  play: async ({ canvas, args }) => {
    const svg = canvas.getByTestId("constellation-svg");
    const { clientX, clientY, point } = loneStar(svg, args.sky);
    await fireEvent.pointerMove(svg, { clientX, clientY });
    const tooltip = await canvas.findByRole("tooltip");
    const dir = args.sky.dirs[point.dir_cluster];
    await expect(tooltip).toHaveTextContent(dir?.path ? `${dir.path}/` : "(root)");
    await expect(tooltip).toHaveTextContent(/\d+ files?/);

    await fireEvent.click(svg, { clientX, clientY });
    const card = await canvas.findByRole("region", { name: `File preview: ${point.path}` });
    await expect(card).toHaveTextContent(point.path.slice(point.path.lastIndexOf("/") + 1));
    const open = within(card).getByRole("link", { name: "Open in code" });
    await expect(open).toHaveAttribute(
      "href",
      `/acme/api/code/${point.path.split("/").map(encodeURIComponent).join("/")}`,
    );
    await fireEvent.click(within(card).getByRole("button", { name: "Close the preview" }));
    await waitFor(() => expect(canvas.queryByRole("region", { name: /File preview/ })).toBeNull());
  },
};

/** The accessible alternative: every directory with files, largest first, linked to the code. */
export const AsAList: Story = {
  play: async ({ canvas }) => {
    await fireEvent.click(canvas.getByRole("button", { name: "Show as a list" }));
    const table = await canvas.findByRole("table", { name: "Directories in the constellation" });
    const rows = within(table).getAllByRole("row");
    await expect(rows.length).toBeGreaterThan(10);
    await expect(
      within(table).getByRole("link", { name: "backend/app/api/routes/" }),
    ).toBeVisible();
    await expect(canvas.getByRole("button", { name: "Hide directory list" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  },
};

/** Discovery part way: what it has found so far. */
export const Forming: Story = {
  args: { sky: fixture(120), live: true },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("forming")).toBeVisible();
    await expect(canvas.getByRole("img", { name: /120 of 245 files found so far/ })).toBeVisible();
  },
};

/** Before the walk is done, and a run that ended before discovery found anything. */
export const Waiting: Story = {
  args: { sky: { ...fixture(0), status: "pending", total: null, dirs: [] } },
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Stars appear as discovery finds the files.")).toBeVisible();
  },
};

export const Stopped: Story = {
  args: { sky: { ...fixture(0), status: "stopped", total: null, dirs: [] } },
  play: async ({ canvas }) => {
    await expect(
      canvas.getByText("This run ended before discovery found its files."),
    ).toBeVisible();
  },
};

/** 50,000 made-up files in the SVG fallback: still a handful of elements. */
export const LargeStatic: Story = {
  args: { sky: syntheticSky(50_000) },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("img", { name: /50,000 files/ })).toBeVisible();
    const svg = canvas.getByTestId("constellation-svg");
    await expect(svg.querySelectorAll("path").length).toBeLessThan(20);
  },
};

/** Frames counted since the meter last showed a number (one meter on screen at a time). */
const frames = { count: 0, ms: 0 };

/** Count a frame that took `ms`; once a second has gone by, the frames per second over it. */
function countFrame(ms: number): number | null {
  frames.count += 1;
  frames.ms += ms;
  if (frames.ms < 1000) return null;
  const fps = Math.round((frames.count * 1000) / frames.ms);
  frames.count = 0;
  frames.ms = 0;
  return fps;
}

/** Frames per second of the 3D view, from the frames it draws. */
function FpsMeter({ children }: { children: (onFrame: (ms: number) => void) => React.ReactNode }) {
  const [fps, setFps] = useState<number | null>(null);
  const onFrame = (ms: number) => {
    const measured = countFrame(ms);
    if (measured !== null) setFps(measured);
  };
  return (
    <div className="flex flex-col gap-2">
      <output aria-label="Frames per second" className="font-mono text-sm tabular-nums">
        {fps === null ? "measuring…" : `${String(fps)} fps`}
      </output>
      {children(onFrame)}
    </div>
  );
}

/**
 * The performance check: 50,000 stars in WebGL, turning (so a frame is drawn every frame), with
 * an FPS meter; the target is at least 50 fps on an M-series MacBook. Needs a GPU, so it is left
 * out of the test run.
 */
export const Performance50k: Story = {
  tags: ["!test"],
  args: { sky: syntheticSky(50_000), live: true, renderer: "webgl" },
  parameters: { constellationRenderer: "webgl" },
  render: (args) => (
    <FpsMeter>{(onFrame) => <ConstellationView {...args} onFrame={onFrame} />}</FpsMeter>
  ),
};

/** The 3D view of the fixture commit, live (needs WebGL2; left out of the test run). */
export const WebGL: Story = {
  tags: ["!test"],
  args: { sky: fixture(), live: true, renderer: "webgl" },
};

/** Put a job on the seeded repository, queued `ago` ms before the story starts. */
function seedJob(id: string, ago: number) {
  resetMockDb();
  const now = Date.now() - ago;
  db.jobs[API_REPO_ID] = [
    { id, repo_id: API_REPO_ID, created_at: new Date(now).toISOString(), queued_ms: now },
  ];
  saveDb();
}

/** Fetched from the (mock) API for a finished run: every page of it. */
export const FromTheApi: StoryObj<typeof Constellation> = {
  render: () => <Constellation org="acme" repo="api" job="job-constellation" />,
  beforeEach: () => {
    seedJob("job-constellation", RUN_LENGTH_MS + 60_000);
  },
  play: async ({ canvas }) => {
    await expect(
      await canvas.findByRole("img", { name: /Constellation of the repository: 245 files/ }),
    ).toBeVisible();
  },
};
