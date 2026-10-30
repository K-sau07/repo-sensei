/**
 * Mock agent runs (4.12): a recorded run of 40 steps on the mock snapshot (the fastapi
 * template), as the API's run detail returns it: round 0, research turns each with the tools
 * it called at once, the evidence pick, the answer turn and its checks. Deterministic, so
 * stories and tests can name its steps.
 */
import type { RunDetail, Step } from "@/lib/api/runs";

export const MOCK_RUN_ID = "0190a1b2-0000-7000-8000-000000000040";
const REPO_ID = "00000000-0000-0000-0000-000000000000";
const MODEL = "claude-sonnet-5-5";
const CREATED = "2026-10-30T12:00:00.000Z";

const READS = [
  "backend/app/api/routes/items.py",
  "backend/app/models.py",
  "backend/app/crud.py",
  "backend/app/api/deps.py",
  "backend/app/core/db.py",
  "backend/app/api/main.py",
];
const QUERIES = ["create item", "item owner", "session add", "ItemCreate", "commit"];

/** The run's steps: `count` of them (40 by default), in the order they were recorded. */
export function mockRunSteps(count = 40): Step[] {
  const steps: Step[] = [];
  let at = 0;
  const add = (
    kind: string,
    name: string,
    input: Record<string, unknown>,
    summary: string,
    ms: number,
    tokens = 0,
    offset = at,
  ) => {
    steps.push({
      idx: steps.length,
      kind,
      name,
      input,
      summary,
      tokens,
      ms,
      offset_ms: offset,
      has_output: kind === "tool",
    });
    return offset + ms;
  };
  at = add("retrieval", "round0", { traced: true }, "18 results in 7 files", 420);
  let turn = 0;
  while (steps.length < count - 4) {
    turn += 1;
    at = add("llm", MODEL, { turn, effort: "medium", tools: 9 }, "", 1800, 2400 + turn * 310);
    // The turn's calls, made at once: side by side in the waterfall.
    const calls = turn % 3 === 0 ? 1 : 3;
    let end = at;
    for (let i = 0; i < calls && steps.length < count - 4; i += 1) {
      const n = turn * 3 + i;
      const [name, input, summary] =
        i === 0
          ? ["code_search", { query: QUERIES[n % QUERIES.length] }, "6 results in 4 files"]
          : [
              "read_code",
              { path: READS[n % READS.length], start_line: 1, end_line: 80 },
              "80 lines",
            ];
      end = Math.max(end, add("tool", name, input, summary, 120 + ((n * 37) % 400), 0, at));
    }
    at = end;
  }
  at = add(
    "tool",
    "submit_evidence",
    { candidates: READS.slice(0, 2).map((target) => ({ target, start_line: 1, end_line: 40 })) },
    "2 candidates",
    20,
  );
  at = add("retrieval", "evidence", {}, "6 pieces, 2 submitted", 380);
  at = add("llm", MODEL, { turn: turn + 1, effort: "medium", tools: 0 }, "", 3100, 5200);
  add("verify", "verify", { rounds: 0 }, "2 of 2 cited claims supported", 900, 0, at);
  return steps;
}

export function mockRun(steps: Step[] = mockRunSteps(), status = "succeeded"): RunDetail {
  const last = Math.max(...steps.map((s) => s.offset_ms + s.ms));
  const tokens = steps.reduce((sum, s) => sum + s.tokens, 0);
  return {
    id: MOCK_RUN_ID,
    repo_id: REPO_ID,
    kind: "ask",
    question: "How is an item created?",
    model: MODEL,
    mode: "fast",
    status,
    stopped: null,
    user: "octo",
    turns: steps.filter((s) => s.kind === "llm").length,
    tokens_in: Math.round(tokens * 0.9),
    tokens_out: Math.round(tokens * 0.1),
    cache_read: 0,
    cache_write: 0,
    cost_usd: "0.0832",
    latency_ms: status === "running" ? null : last,
    first_token_ms: 2400,
    error: null,
    created_at: CREATED,
    finished_at: status === "running" ? null : CREATED,
    steps: steps.map((s) => ({
      ...s,
      output: s.has_output ? [{ type: "text", text: s.summary }] : null,
    })),
  };
}
