/**
 * The product pages' examples (10.3): what the engine made of a real open-source repository,
 * fastapi/full-stack-fastapi-template at b1125d8 (the trace fixture: its `trace_feature` from
 * the Login page, recorded by scripts/mock_trace.py). Read on the server; a page sends only
 * what it shows.
 */

import type { Spec } from "@/lib/api/diagrams";
import { pipOf, type Pip, type Trace, type TracePath } from "@/lib/api/trace";
import { mermaid, ratio, specOfTrace } from "@/lib/diagrams/from-trace";
import fixture from "@/mocks/fixtures/trace.json";
import { LOGIN_ENTRY } from "@/mocks/trace";

export const DEMO_REPO = "fastapi/full-stack-fastapi-template";
export const DEMO_SHA = "b1125d8";

interface Fixture {
  aliases: Record<string, string>;
  traces: Record<string, Trace>;
}

function loginTrace(): Trace {
  const traces = fixture as unknown as Fixture;
  const trace = traces.traces[traces.aliases[LOGIN_ENTRY] ?? LOGIN_ENTRY];
  if (!trace) throw new Error("The trace fixture has no Login trace.");
  return trace;
}

/** Signing in, from the Login page to the user table and back. */
export function demoTracePath(): TracePath {
  const path = loginTrace().paths[0];
  if (!path) throw new Error("The Login trace has no path.");
  return path;
}

/** The request half of the same trace (the Login page down to the tables it reads), drawn as a
 * diagram: every edge one the trace found (all verified). */
export function demoDiagram(): { spec: Spec; mermaid: string; verifiedRatio: number } {
  const trace = loginTrace();
  const path = demoTracePath();
  const firstData = path.hops.findIndex((hop) => hop.layer === "db");
  const afterData = path.hops.findIndex((hop, i) => i > firstData && hop.layer !== "db");
  const hops = firstData < 0 || afterData < 0 ? path.hops : path.hops.slice(0, afterData);
  const spec = specOfTrace(
    { ...trace, paths: [{ ...path, hops }] },
    "Signing in",
    "flowchart",
    false,
  );
  return { spec, mermaid: mermaid(spec), verifiedRatio: ratio(spec) };
}

export interface StoryHop {
  name: string;
  layer: string;
  file: string;
  line: number | null;
  /** How sure the hop is (the trace view's pip): none for the entry. */
  pip: Pip | null;
}

/** The landing page's scroll story (10.3): the request half of the same sign-in trace, hop by
 * hop, with each one's file, line and how sure it is. */
export function storyHops(): StoryHop[] {
  const path = demoTracePath();
  const firstData = path.hops.findIndex((hop) => hop.layer === "db");
  const afterData = path.hops.findIndex((hop, i) => i > firstData && hop.layer !== "db");
  const hops = firstData < 0 || afterData < 0 ? path.hops : path.hops.slice(0, afterData);
  return hops.map((hop) => ({
    name: hop.node.name,
    layer: hop.layer,
    file: hop.node.path?.split("/").at(-1) ?? "",
    line: hop.node.lines?.[0] ?? null,
    pip: pipOf(hop.edge ?? null),
  }));
}
