import type { Decorator, Meta, StoryObj } from "@storybook/nextjs-vite";
import { MotionConfig } from "motion/react";
import { http, HttpResponse } from "msw";
import { NuqsTestingAdapter } from "nuqs/adapters/testing";
import { expect, userEvent, waitFor, within } from "storybook/test";

import { TracePage } from "@/components/trace/trace-page";
import type { Trace } from "@/lib/api/trace";
import { BROWSE_COMMIT } from "@/mocks/browse";
import fixture from "@/mocks/fixtures/trace.json";
import { handlers } from "@/mocks/handlers";
import { AMBIGUOUS_ENTRY, GAP_ENTRY, LOGIN_ENTRY } from "@/mocks/trace";

import { expectCodeLine } from "../code-lines";

// The viewer's editor loads as its own chunk: on a busy machine that can take a while.
const VIEWER_MS = 5000;

const nextjs = {
  appDirectory: true,
  navigation: {
    pathname: "/acme/api/trace",
    segments: [
      ["org", "acme"],
      ["repo", "api"],
    ],
  },
};

/** The trace page at `?from=` (none: the entry picker). */
const at = (search: string): Decorator[] => [
  (Story) => (
    <NuqsTestingAdapter searchParams={search}>
      <Story />
    </NuqsTestingAdapter>
  ),
];

const meta = {
  title: "Trace/Trace view",
  component: TracePage,
  parameters: { nextjs, msw: handlers },
} satisfies Meta<typeof TracePage>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The hops listed (and the gap, if any). */
const hops = async (root: HTMLElement) =>
  within(await within(root).findByRole("list", { name: "Hops, in order" })).getAllByRole(
    "listitem",
  );

const UNLINKED = (fixture.unlinked as unknown as Record<string, Trace>)[LOGIN_ENTRY];

/** The Login page, resolved down to the user table: three paths, the best one shown. */
export const Resolved: Story = {
  decorators: at(`?from=${encodeURIComponent(LOGIN_ENTRY)}`),
  play: async ({ canvas, canvasElement }) => {
    await expect(await hops(canvasElement)).toHaveLength(9);
    const tabs = canvas.getByRole("tablist", { name: "Paths, best first" });
    await expect(within(tabs).getAllByRole("tab")).toHaveLength(3);
    await expect(canvas.getByText("Reaches the user table")).toBeVisible();
    await expect(canvas.getByTestId("trace-beam")).toBeInTheDocument();
    // A hop's code opens below, at the line that proves it.
    await userEvent.click(canvas.getByRole("button", { name: /^Step 7 of 9, Data/ }));
    const code = await canvas.findByRole("region", { name: "Code of authenticate" });
    await expectCodeLine(code, /crud\.authenticate/, VIEWER_MS);
  },
};

/** A call no endpoint matches: the path stops in the UI, with the reason, and a way to ask. */
export const PartialWithGap: Story = {
  decorators: at(`?from=${encodeURIComponent(GAP_ENTRY)}`),
  play: async ({ canvas, canvasElement }) => {
    await expect(await hops(canvasElement)).toHaveLength(4); // three hops and the gap
    const gap = canvas.getByRole("group", { name: "Gap in the trace" });
    await expect(gap).toHaveTextContent("client call has no matching endpoint");
    await expect(gap).toHaveTextContent("no POST endpoint at /api/v1/items/export");
    await expect(within(gap).getByRole("button", { name: /Help me resolve/ })).toBeVisible();
    await expect(
      within(canvas.getByRole("tabpanel")).getByText(/^Stops: client call/),
    ).toBeVisible();
  },
};

/** A URL two routes match: a path through each, the endpoint hop marked "1 of 2". */
export const AmbiguousEndpoint: Story = {
  decorators: at(`?from=${encodeURIComponent(AMBIGUOUS_ENTRY)}`),
  play: async ({ canvas }) => {
    const endpoint = within(await canvas.findByRole("list", { name: "Hops, in order" })).getByRole(
      "button",
      { name: /^Step 3 of 5, Endpoint/ },
    );
    await expect(endpoint).toHaveTextContent("ambiguous, 1 of 2");
    await expect(canvas.getAllByRole("tab")).toHaveLength(3);
  },
};

/** With reduced motion the beam is the resting path from the start: no pulse runs. */
export const ReducedMotion: Story = {
  decorators: [
    (Story) => (
      <MotionConfig reducedMotion="always">
        <Story />
      </MotionConfig>
    ),
    ...at(`?from=${encodeURIComponent(LOGIN_ENTRY)}`),
  ],
  play: async ({ canvas, canvasElement }) => {
    await expect(await hops(canvasElement)).toHaveLength(9);
    await waitFor(async () => {
      await expect(canvas.getByTestId("trace-beam")).toHaveAttribute("data-pulse", "off");
    });
    await expect(canvas.getByTestId("trace-beam").querySelector("[data-beam=pulse]")).toBeNull();
  },
};

/** Before the links stage: the trace stays in the code, and says so. */
export const Unlinked: Story = {
  decorators: at(`?from=${encodeURIComponent(LOGIN_ENTRY)}&view=list`),
  parameters: {
    msw: [
      http.get("*/api/v1/orgs/:org/repos/:repo/trace", () =>
        HttpResponse.json<Trace>(UNLINKED ?? null, { headers: { "X-Commit-Sha": BROWSE_COMMIT } }),
      ),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByText("This trace stays in the code.")).toBeVisible();
    await expect(canvas.getByTestId("trace-list")).toBeVisible();
  },
};

/** No `?from=`: pick where the trace starts. */
export const EntryPicker: Story = {
  decorators: at(""),
  play: async ({ canvas }) => {
    await userEvent.type(canvas.getByRole("textbox", { name: "Where the trace starts" }), "Login");
    const found = await canvas.findByRole("list", { name: "Symbols to trace from" });
    await expect(within(found).getByRole("link", { name: /^Login ?component/ })).toHaveAttribute(
      "href",
      expect.stringContaining("/acme/api/trace?from=") as string,
    );
  },
};
