import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, fn, userEvent, within } from "storybook/test";

import { AbstainCard } from "@/components/ask/abstain-card";
import { Answer } from "@/components/ask/answer";
import { ConfidenceBadge } from "@/components/ask/confidence-badge";
import type { AbstainData, AskMessage, ConfidenceData } from "@/lib/api/ask";
import { citedKey } from "@/lib/api/refs";
import { MOCK_SHA, mockAbstainParts, mockAnswerParts } from "@/mocks/ask";
import { PUSHED_SHA } from "@/mocks/push";

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

/** A streamed answer's parts as the client keeps them once it is done. */
function kept(parts: Record<string, unknown>[]): AskMessage {
  const text = parts
    .filter((p) => p.type === "text-delta")
    .map((p) => String(p.delta))
    .join("");
  const rest = parts.filter(
    (p) => String(p.type).startsWith("data-") || p.type === "tool-input-available",
  );
  return {
    id: "m1",
    role: "assistant",
    parts: [
      { type: "text", text },
      ...rest.map((p) =>
        p.type === "tool-input-available"
          ? {
              type: `tool-${String(p.toolName)}`,
              toolCallId: String(p.toolCallId),
              state: "output-available",
              input: p.input,
              output: {},
            }
          : p,
      ),
    ] as AskMessage["parts"],
  };
}

const CITED = kept(mockAnswerParts("c1"));
const ABSTAINED = kept(mockAbstainParts("c2"));

const breakdown = {
  claims_total: 5,
  claims_cited: 5,
  claims_verified: 4,
  claims_partial: 1,
  inferred: 1,
  unresolved_hops: 0,
  agreement: 0.8,
  unknown_identifiers: 0,
  dropped_citations: 0,
  whole: true,
};
const sure = (level: ConfidenceData["level"], score: number): ConfidenceData => ({
  level,
  score,
  breakdown,
});

const meta = {
  title: "Ask/Answer",
  component: Answer,
  parameters: { nextjs: navigation },
  args: { message: CITED, streaming: false, feedback: null },
} satisfies Meta<typeof Answer>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A finished answer: chips after the words they back, its confidence, feedback buttons. */
export const Cited: Story = {
  play: async ({ canvas }) => {
    await expect(canvas.getAllByTestId("citation-chip")).toHaveLength(2);
    await expect(canvas.getByTestId("confidence")).toHaveTextContent("Confidence: high");
    await expect(canvas.getByRole("button", { name: "Helpful" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  },
};

/** While it is written: what the run is doing, no footer yet. */
export const Streaming: Story = {
  args: {
    streaming: true,
    message: { ...CITED, parts: CITED.parts.filter((p) => p.type.startsWith("tool-")) },
  },
  play: async ({ canvas }) => {
    await expect(canvas.getByTestId("status-line")).toHaveTextContent("Searching code");
    await expect(canvas.queryByTestId("confidence")).toBeNull();
  },
};

/** The run couldn't stand behind it: why, where the trail ended, a narrower question. */
export const Abstained: Story = {
  args: { message: ABSTAINED, onAsk: () => undefined },
  play: async ({ canvas }) => {
    const card = canvas.getByTestId("abstain-card");
    await expect(card).toHaveTextContent("I couldn't verify this");
    await expect(card).toHaveTextContent("POST /items/");
    await expect(
      within(card).getByRole("button", { name: "Ask: Where are items deleted?" }),
    ).toBeVisible();
    await expect(canvas.getByTestId("confidence")).toHaveTextContent("Confidence: low");
  },
};

/** Marked wrong before: the thumbs-down is pressed. */
export const WithFeedback: Story = {
  args: { feedback: "wrong_file" },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("button", { name: "Something's wrong" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  },
};

/** A file it cites changed since (5.6): its chip says so, and so does the answer, with a way to
 * ask it again on the latest index. */
export const Stale: Story = {
  args: {
    staleness: {
      checked: new Map([
        [citedKey(MOCK_SHA, "backend/app/models.py"), "changed"],
        [citedKey(MOCK_SHA, "backend/app/api/routes/items.py"), "unchanged"],
      ]),
      head: PUSHED_SHA,
    },
    onRerun: fn(),
  },
  play: async ({ args, canvas }) => {
    const stale = canvas.getByTestId("answer-stale");
    await expect(stale).toHaveTextContent("1 cited file changed since");
    const chips = canvas.getAllByTestId("citation-chip");
    await expect(chips.filter((chip) => chip.dataset.stale === "changed")).toHaveLength(1);
    await userEvent.click(within(stale).getByRole("button", { name: "Re-run on latest" }));
    await expect(args.onRerun).toHaveBeenCalledOnce();
  },
};

/** On a shared page for someone who can't read the repository: each citation is a number. */
export const Redacted: Story = {
  args: {
    feedback: undefined,
    message: {
      ...CITED,
      parts: CITED.parts.flatMap((p) => {
        if (p.type === "text" || p.type === "data-confidence") return [p];
        if (p.type !== "data-citation") return [];
        const { n, span } = p.data;
        return [{ ...p, data: { n, span, redacted: true } }] as AskMessage["parts"];
      }),
    },
    chip: (c) => (
      <span data-testid="citation-chip" className="font-mono text-xs">
        {c.n} <span className="sr-only">Citation hidden</span>
      </span>
    ),
  },
  play: async ({ canvas }) => {
    await expect(canvas.getAllByTestId("citation-chip")).toHaveLength(2);
    await expect(canvas.queryByText(/items\.py/)).toBeNull();
    await expect(canvas.queryByTestId("feedback")).toBeNull();
    await expect(canvas.getByText(/An item is created by the/)).toBeVisible();
  },
};

export const ConfidenceLevels: StoryObj = {
  render: () => (
    <div className="flex gap-4 text-xs">
      <ConfidenceBadge confidence={sure("high", 0.92)} />
      <ConfidenceBadge confidence={sure("medium", 0.6)} />
      <ConfidenceBadge confidence={sure("low", 0.2)} />
    </div>
  ),
  play: async ({ canvas }) => {
    const [high] = canvas.getAllByTestId("confidence");
    if (high) await userEvent.click(high);
    const body = within(document.body);
    const breakdownShown = await body.findByTestId("confidence-breakdown");
    await expect(breakdownShown).toHaveTextContent("4/5 claims verified against the code");
    await expect(breakdownShown).toHaveTextContent("1 inferred from naming");
  },
};

const ABSTAIN: AbstainData = {
  reason: "Nothing it read handles a refund.",
  searched: [],
  trailEnd: null,
  suggestion: "",
};

/** An abstention with nothing more to say: no trail, no suggestion. */
export const AbstainBare: StoryObj = {
  render: () => <AbstainCard abstain={ABSTAIN} />,
  play: async ({ canvas }) => {
    await expect(canvas.getByTestId("abstain-card")).toHaveTextContent("Nothing it read");
    await expect(canvas.queryByRole("button")).toBeNull();
  },
};
