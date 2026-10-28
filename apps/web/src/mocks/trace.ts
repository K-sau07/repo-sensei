/**
 * The mock snapshot's traces (fixtures/trace.json: the engine's `trace_feature` over the template,
 * linked in-process by scripts/mock_trace.py, as `GET …/trace` sends them), loaded on first use,
 * by entry: a node's key, or another name for the same entry (its file, its name). `unlinked` has
 * the Login page's trace before the links stage, as the API answers it then (`linked: false`).
 */
import type { Trace } from "@/lib/api/trace";

interface Fixture {
  aliases: Record<string, string>;
  traces: Record<string, Trace>;
  unlinked: Record<string, Trace>;
}

export interface MockTraces {
  /** The trace from `entry` (null: nothing in the mock's graph is that entry). */
  find: (entry: string, linked: boolean) => Trace | null;
  /** The entries the mock can trace, by their keys. */
  entries: readonly string[];
}

/** The Login page: the trace from the UI to the user table (and the 3.14 "t" of a component). */
export const LOGIN_ENTRY = "tsx frontend/src/routes/login.tsx/Login().";
/** A call no endpoint matches: the trace stops in the UI, with the reason. */
export const GAP_ENTRY = "tsx frontend/src/components/Items/ItemsReport.tsx/ExportItems().";
/** A call two endpoints match: a path through each. */
export const AMBIGUOUS_ENTRY = "tsx frontend/src/components/Items/ItemsReport.tsx/DailyReport().";

let loaded: Promise<MockTraces> | null = null;

function index(fixture: Fixture): MockTraces {
  return {
    find: (entry, linked) => {
      const key = fixture.aliases[entry] ?? entry;
      return (linked ? fixture.traces[key] : fixture.unlinked[key]) ?? null;
    },
    entries: Object.keys(fixture.traces),
  };
}

export function mockTraces(): Promise<MockTraces> {
  loaded ??= import("./fixtures/trace.json").then((module) =>
    index(module.default as unknown as Fixture),
  );
  return loaded;
}
