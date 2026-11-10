/** What changed, newest first (10.5): each release with the day it shipped and what it brought. */
export interface Release {
  date: string; // ISO day
  title: string;
  changes: readonly string[];
}

export const RELEASES: readonly Release[] = [
  {
    date: "2026-11-10",
    title: "Benchmarks, speed and safety",
    changes: [
      "CrossLayerBench: 101 hand-labelled flows over five full-stack repositories, and how the tracer does on them, published.",
      "Insights say how fast the app's pages were for your team, against their budgets.",
      "Answers never load images, and every agent treats a repository's text as data.",
      "The privacy page now names every provider that handles data, page-speed measurements and backups; the terms add limits and liability; privacy and security have their own addresses.",
    ],
  },
  {
    date: "2026-11-04",
    title: "The public site, Explore and settings",
    changes: [
      "A landing page with a live demo, product pages, docs, pricing, and the security, privacy and terms pages.",
      "Explore: open-source repositories, indexed and public, to read and ask about without an account.",
      "Organization settings for usage against caps, the audit log, integrations, and deleting or restoring an organization.",
      "Appearance (accent, contrast, density, motion and transparency) and a keyboard shortcuts page.",
    ],
  },
  {
    date: "2026-11-03",
    title: "Changes, notifications and limits",
    changes: [
      "Ask for a change in plain words: it is planned by layer, approved, patched with new tests and verified.",
      "An inbox and notifications by email and Slack for runs, approvals and indexing.",
      "Public imports for anyone, with size and daily limits and a spending cap.",
    ],
  },
  {
    date: "2026-11-03",
    title: "Fixes",
    changes: [
      "From a stack trace, a GitHub issue or a Sentry error: investigated, reproduced by a failing test, planned, approved by people with write access, patched and verified in a sandbox.",
      "Patches that game the tests (editing or skipping them, faking reports) are refused.",
    ],
  },
  {
    date: "2026-11-01",
    title: "MCP, the command line and API keys",
    changes: [
      "An MCP server for Claude Code, VS Code, Cursor and other clients, signed in with OAuth or an API key.",
      "The sensei command line: sign in, connect, search, ask and trace.",
      "API keys with scopes, per organization.",
    ],
  },
  {
    date: "2026-11-01",
    title: "Diagrams and the graph",
    changes: [
      "Diagrams drawn from the code's graph, each edge checked, with the share verified on them.",
      "A graph explorer, dependencies by level, cycles and what breaks them, packages, and a map of the whole codebase.",
      "Impact: what a change to a symbol reaches, before you make it.",
    ],
  },
  {
    date: "2026-10-31",
    title: "Staying fresh",
    changes: [
      "Each push is indexed again, only what changed.",
      "Tracked branches and pull requests, with a branch picker.",
      "Answers, traces and diagrams say when the code they cite has changed since.",
    ],
  },
  {
    date: "2026-10-30",
    title: "Ask",
    changes: [
      "Questions about the code, each claim citing its lines and checked against them.",
      "Answers that say when the code doesn't tell; scope chips and @ mentions; sharing; history; ⌘L from anywhere.",
    ],
  },
  {
    date: "2026-10-29",
    title: "Trace across layers",
    changes: [
      "The code's graph, with frontend calls matched to the routes that serve them.",
      "Trace a feature from the page to the database, with its gaps and why.",
    ],
  },
  {
    date: "2026-10-26",
    title: "Search",
    changes: ["Exact, keyword and meaning search, symbols and go to definition."],
  },
  {
    date: "2026-10-21",
    title: "Repositories and indexing",
    changes: [
      "Connect repositories through the GitHub App or by URL, and watch them index stage by stage.",
      "A code browser for repositories of any size.",
    ],
  },
  {
    date: "2026-10-19",
    title: "Foundations",
    changes: ["Sign in with GitHub, organizations, invitations and roles."],
  },
];
