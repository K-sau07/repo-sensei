/**
 * The landing page's sections below the demo (10.3): what it guarantees, what it does, how it
 * keeps code safe, the editors it works in, and where to start. Every line here is something
 * the product does today.
 */
import {
  BookOpenCheck,
  GitBranch,
  Network,
  Radar,
  ShieldCheck,
  Spline,
  Waypoints,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";

import { buttonVariants } from "@/components/ui/button";
import { CLIENT_LABELS, MCP_CLIENTS } from "@/lib/mcp-setup";

import { Spotlight } from "./spotlight";

const GUARANTEES = [
  { title: "Cites file and line", body: "Each claim points at the lines it stands on, at the commit it read." },
  { title: "Says when it can't tell", body: "When the code doesn't answer a question, the answer says so." },
  { title: "Fixes start from a failing test", body: "A bug is reproduced first; a patch counts once that test passes." },
  { title: "Fresh on every push", body: "Only what changed is indexed again; branches and pull requests too." },
] as const; // prettier-ignore

export function ProofStrip() {
  return (
    <section aria-label="What every answer keeps to">
      <ul className="grid gap-px overflow-hidden rounded-xl border border-line bg-line sm:grid-cols-2 lg:grid-cols-4">
        {GUARANTEES.map((item) => (
          <li key={item.title} className="flex flex-col gap-1 bg-bg p-5">
            <p className="font-medium">{item.title}</p>
            <p className="text-sm text-fg-muted">{item.body}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}

interface Feature {
  icon: LucideIcon;
  title: string;
  body: string;
  wide?: boolean;
}

// In rows of three: a wide one beside one, three, then a wide one beside one.
const FEATURES: readonly Feature[] = [
  {
    icon: BookOpenCheck,
    title: "Ask",
    body: "Questions about the code, answered from the code: each claim checked against the lines it cites, and each citation says whether they hold it up.",
    wide: true,
  },
  {
    icon: Waypoints,
    title: "Trace",
    body: "Follow a feature from the button to the database: UI, API, services and storage, hop by hop.",
  },
  {
    icon: Spline,
    title: "Diagrams",
    body: "Drawn from the code's graph, each edge checked, and marked when it no longer matches.",
  },
  {
    icon: Network,
    title: "Dependencies and impact",
    body: "Modules, packages and cycles, and what a change to a symbol reaches before you make it.",
  },
  {
    icon: GitBranch,
    title: "Branches and pull requests",
    body: "Read any tracked branch; answers say when what they cited has changed since.",
  },
  {
    icon: Wrench,
    title: "Fix and change",
    body: "From a stack trace or an issue: investigated, reproduced by a failing test, planned for your approval, patched in a sandbox and verified by the tests. A change asked for in plain words is planned, approved and tested the same way.",
    wide: true,
  },
  {
    icon: Radar,
    title: "Explore",
    body: "Open-source repositories, indexed and public, to read and ask about without an account.",
  },
];

export function Features() {
  return (
    <section aria-labelledby="features-title" className="flex flex-col gap-6">
      <h2 id="features-title" className="text-2xl font-semibold tracking-tight">
        One index of the code, many ways to read it
      </h2>
      <Spotlight>
        <ul className="grid gap-4 md:grid-cols-3">
          {FEATURES.map((feature) => (
            <li
              key={feature.title}
              data-spotlight
              className={`flex flex-col gap-2 overflow-hidden rounded-xl border border-line bg-surface-1/70 p-5 transition-colors hover:border-line-strong ${feature.wide ? "md:col-span-2" : ""}`}
            >
              <feature.icon aria-hidden className="size-5 text-signal" />
              <h3 className="font-medium">{feature.title}</h3>
              <p className="text-sm text-fg-muted">{feature.body}</p>
            </li>
          ))}
        </ul>
      </Spotlight>
    </section>
  );
}

const SAFEGUARDS = [
  "Private code is read through a GitHub App, installed only on the repositories you choose.",
  "Each organization's data is kept apart in the database itself, row by row.",
  "Tests run in a sandbox with no internet access; only installing dependencies reaches the package registries.",
  "Nothing is changed without a person approving the plan; risky plans need two.",
  "Agent runs stop at a daily spending cap, and summaries at a monthly budget.",
  "Changes to members, repositories, keys, integrations and fixes are in the audit log.",
] as const;

export function Safety() {
  return (
    <section
      aria-labelledby="safety-title"
      className="grid gap-6 rounded-2xl border border-line p-6 md:grid-cols-[1fr_2fr] md:p-10"
    >
      <div className="flex flex-col gap-2">
        <ShieldCheck aria-hidden className="size-6 text-signal" />
        <h2 id="safety-title" className="text-2xl font-semibold tracking-tight">
          Your code stays yours
        </h2>
      </div>
      <ul className="flex flex-col gap-3 text-sm">
        {SAFEGUARDS.map((line) => (
          <li key={line} className="flex gap-2">
            <span aria-hidden className="mt-2 size-1.5 shrink-0 rounded-full bg-signal" />
            <span>{line}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function Editors() {
  return (
    <section
      aria-labelledby="editors-title"
      className="flex flex-col items-center gap-4 text-center"
    >
      <h2 id="editors-title" className="text-2xl font-semibold tracking-tight">
        In your editor, through MCP
      </h2>
      <p className="max-w-xl text-sm text-fg-muted">
        Search, trace and ask from your coding agent, signed in once with OAuth or an API key.
      </p>
      <ul className="flex flex-wrap justify-center gap-2" aria-label="Editors with setup steps">
        {MCP_CLIENTS.map((client) => (
          <li
            key={client}
            className="rounded-full border border-line px-4 py-1.5 font-mono text-sm text-fg-muted"
          >
            {CLIENT_LABELS[client]}
          </li>
        ))}
        <li className="rounded-full border border-dashed border-line px-4 py-1.5 text-sm text-fg-faint">
          any MCP client
        </li>
      </ul>
    </section>
  );
}

export function Closing() {
  return (
    <section
      aria-labelledby="closing-title"
      className="flex flex-col items-center gap-4 rounded-2xl bg-surface-2 px-6 py-12 text-center"
    >
      <h2 id="closing-title" className="text-2xl font-semibold tracking-tight">
        Start with one repository
      </h2>
      <p className="max-w-md text-sm text-fg-muted">
        Sign in with GitHub, connect a repository, and ask it the first question you have.
      </p>
      <div className="flex flex-wrap justify-center gap-2">
        <Link href="/login" className={buttonVariants({ size: "lg" })}>
          Get started
        </Link>
        <Link href="/explore" className={buttonVariants({ size: "lg", variant: "outline" })}>
          Explore first
        </Link>
      </div>
    </section>
  );
}
