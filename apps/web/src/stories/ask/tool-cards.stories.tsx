import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import type { Route } from "next";
import { expect, fn, userEvent, within } from "storybook/test";

import { Answer } from "@/components/ask/answer";
import { ArtifactPanel, useArtifacts } from "@/components/ask/artifact-panel";
import { ToolCards, type CodeHref } from "@/components/ask/tool-cards";
import type { AskMessage } from "@/lib/api/ask";
import { cardsOf, type CardSlot, type ToolCard } from "@/lib/api/cards";
import { MOCK_ANSWER, MOCK_CARDS } from "@/mocks/ask";

const navigation = {
  appDirectory: true,
  navigation: {
    pathname: "/acme/api/ask",
    segments: [
      ["org", "acme"],
      ["repo", "api"],
    ],
  },
};

const codeHref: CodeHref = (path, [start, end]) =>
  `/acme/api/code/${path}?L=${String(start)}-${String(end)}` as Route;
const traceHref = (entry: string) => `/acme/api/trace?from=${encodeURIComponent(entry)}` as Route;

const ready = (id: string, tool: string, card: unknown): CardSlot => ({
  id,
  tool,
  state: "ready",
  card: card as ToolCard,
  summary: "",
});

const SLOTS: CardSlot[] = [
  ready("t4", "find_symbol", MOCK_CARDS.symbols),
  ready("t5", "graph_neighbors", MOCK_CARDS.graph),
  ready("t6", "trace_feature", MOCK_CARDS.trace),
];

const meta = {
  title: "Ask/Tool cards",
  component: ToolCards,
  parameters: { nextjs: navigation },
  args: { slots: SLOTS, codeHref, traceHref, onPromote: fn() },
} satisfies Meta<typeof ToolCards>;

export default meta;
type Story = StoryObj<typeof meta>;

/** What the run found: two definitions, the handler's neighbours, the traced path and the
 * endpoint it passes through. Names link into the code at their lines. */
export const Found: Story = {
  play: async ({ canvas, args }) => {
    const definitions = canvas.getByRole("region", { name: "Definitions (2)" });
    await expect(
      within(definitions).getByRole("link", { name: "backend/app/crud.py:63–68" }),
    ).toHaveAttribute("href", "/acme/api/code/backend/app/crud.py?L=63-68");
    const path = canvas.getByRole("list", { name: "Traced path" });
    await expect(within(path).getAllByRole("listitem")).toHaveLength(5);
    await expect(canvas.getByRole("link", { name: "Open in Trace" })).toHaveAttribute(
      "href",
      "/acme/api/trace?from=AddItem",
    );
    const endpoints = canvas.getByRole("table", { name: "Endpoints" });
    await expect(within(endpoints).getByText("/api/v1/items/")).toBeVisible();
    await userEvent.click(
      canvas.getByRole("button", { name: "Promote Neighbours of create_item to the side panel" }),
    );
    await expect(args.onPromote).toHaveBeenCalledWith({ kind: "card", card: MOCK_CARDS.graph });
  },
};

/** A lookup still running, and one that failed. */
export const LoadingAndFailed: Story = {
  args: {
    slots: [
      { id: "a", tool: "trace_feature", state: "loading", card: null, summary: "" },
      { id: "b", tool: "graph_neighbors", state: "error", card: null, summary: "not in the graph" },
    ],
  },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("status", { name: "Tracing the flow…" })).toBeVisible();
    await expect(
      canvas.getByText("Following the graph found nothing to show: not in the graph"),
    ).toBeVisible();
  },
};

function Panel() {
  const artifacts = useArtifacts();
  const later = {
    ...MOCK_CARDS.graph,
    hops: MOCK_CARDS.graph.hops.slice(0, 1),
  } as unknown as ToolCard;
  return (
    <div className="grid h-[640px] grid-cols-[1fr_22rem] gap-4">
      <div className="flex flex-col gap-2">
        <button
          type="button"
          onClick={() => {
            artifacts.promote({ kind: "card", card: MOCK_CARDS.graph as unknown as ToolCard });
          }}
        >
          Promote the first answer&apos;s graph
        </button>
        <button
          type="button"
          onClick={() => {
            artifacts.promote({ kind: "card", card: later });
          }}
        >
          Promote a later answer&apos;s graph
        </button>
      </div>
      {artifacts.current ? (
        <ArtifactPanel
          pinned={artifacts.pinned}
          current={artifacts.current}
          onShow={artifacts.show}
          onClose={artifacts.close}
          codeHref={codeHref}
        />
      ) : (
        <p>Nothing promoted.</p>
      )}
    </div>
  );
}

/** A card promoted twice, as two answers saw it: versions, the latest shown. */
export const ArtifactVersions: StoryObj<typeof Panel> = {
  render: () => <Panel />,
  play: async ({ canvas }) => {
    await userEvent.click(canvas.getByRole("button", { name: "Promote the first answer's graph" }));
    const panel = await canvas.findByRole("complementary", { name: "Artifact" });
    await expect(within(panel).getByText("Neighbours of create_item")).toBeVisible();
    await expect(within(panel).queryByTestId("artifact-version")).toBeNull();
    // The same card again, unchanged: no new version; changed: a second one.
    await userEvent.click(canvas.getByRole("button", { name: "Promote the first answer's graph" }));
    await expect(within(panel).queryByTestId("artifact-version")).toBeNull();
    await userEvent.click(canvas.getByRole("button", { name: "Promote a later answer's graph" }));
    await expect(within(panel).getByTestId("artifact-version")).toHaveTextContent("v2 of 2");
    await userEvent.click(within(panel).getByRole("button", { name: "Earlier version" }));
    await expect(within(panel).getByTestId("artifact-version")).toHaveTextContent("v1 of 2");
  },
};

const WITH_CARDS: AskMessage = {
  id: "m1",
  role: "assistant",
  parts: [
    { type: "text", text: MOCK_ANSWER },
    ...SLOTS.map((slot) => ({
      type: `tool-${slot.tool}`,
      toolCallId: slot.id,
      state: "output-available",
      input: {},
      output: { summary: "", hits: 1, isError: false, ms: 40, card: slot.card },
    })),
  ] as AskMessage["parts"],
};

/** An answer with its cards and questions to ask next about what they found. */
export const AnswerWithFollowUps: StoryObj<typeof Answer> = {
  render: (args) => <Answer {...args} />,
  args: {
    message: WITH_CARDS,
    streaming: false,
    codeHref,
    traceHref,
    onAsk: fn(),
    asked: ["How is an item created?"],
  },
  play: async ({ canvas, args }) => {
    await expect(cardsOf(WITH_CARDS)).toHaveLength(3);
    const next = canvas.getByRole("navigation", { name: "Ask next" });
    await userEvent.click(within(next).getByRole("button", { name: "Where is `Item` tested?" }));
    await expect(args.onAsk).toHaveBeenCalledWith("Where is `Item` tested?");
  },
};
