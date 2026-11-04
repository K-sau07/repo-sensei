/**
 * The docs (10.5): each page's slug, title and what it covers, in reading order and by section.
 * Metadata only (the pages are MDX in ./docs, loaded by the docs route), so ⌘K can search it
 * anywhere without loading a page.
 */
import type { Route } from "next";

export interface DocEntry {
  slug: string;
  title: string;
  description: string;
  section: "Start" | "Use it" | "Connect tools" | "Trust";
  /** Other words people search for it by. */
  keywords: readonly string[];
}

export const DOCS: readonly DocEntry[] = [
  {
    slug: "getting-started",
    title: "Getting started",
    description: "Sign in, make an organization, connect a repository and ask it something.",
    section: "Start",
    keywords: ["setup", "begin", "first", "onboarding", "sign in"],
  },
  {
    slug: "connecting-repositories",
    title: "Connecting repositories",
    description: "The GitHub App or a public URL, indexing, branches, pull requests and settings.",
    section: "Start",
    keywords: ["github app", "install", "import", "index", "branch", "pull request", "webhook"],
  },
  {
    slug: "ask",
    title: "Ask",
    description: "Questions about the code, citations and what they mean, sharing, and limits.",
    section: "Use it",
    keywords: ["question", "answer", "citation", "verified", "share", "chat"],
  },
  {
    slug: "trace",
    title: "Trace",
    description: "Follow a feature across layers, read gaps, and see what a change reaches.",
    section: "Use it",
    keywords: ["flow", "path", "layers", "impact", "end to end"],
  },
  {
    slug: "mcp",
    title: "MCP in your editor",
    description: "Connect Claude Code, VS Code or Cursor, with OAuth or an API key.",
    section: "Connect tools",
    keywords: ["claude code", "vs code", "vscode", "cursor", "agent", "tools", "oauth", "api key"],
  },
  {
    slug: "cli",
    title: "Command line",
    description: "Install sensei, sign in, and search, ask and trace from a terminal.",
    section: "Connect tools",
    keywords: ["terminal", "sensei", "install", "login", "uv"],
  },
  {
    slug: "security-model",
    title: "Security model",
    description: "Who can read what, how code is kept apart, and what runs where.",
    section: "Trust",
    keywords: ["roles", "permissions", "rls", "sandbox", "keys", "audit", "privacy"],
  },
  {
    slug: "faq",
    title: "FAQ",
    description: "Short answers to the questions people ask first.",
    section: "Trust",
    keywords: ["questions", "help", "languages", "cost", "private"],
  },
];

export const DOC_SECTIONS = ["Start", "Use it", "Connect tools", "Trust"] as const;

export function docPath(slug: string): Route {
  return `/docs/${slug}` as Route;
}

export function docBySlug(slug: string): DocEntry | undefined {
  return DOCS.find((doc) => doc.slug === slug);
}
