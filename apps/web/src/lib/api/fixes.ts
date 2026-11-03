/**
 * Fixes (8.3-8.11): a run from a reported bug to a draft pull request. The API's shapes, and
 * what the pages show of them: each stage's, status's, source's and risk's label, a status's
 * tone, and where a stack frame opens in the code viewer.
 */
import type { Route } from "next";

import { codePath } from "@/lib/routes";

import type { Schemas } from "./types";

export type Fix = Schemas["FixOut"];
export type FixSummary = Schemas["FixSummaryOut"];
export type Ticket = Schemas["TicketOut"];
export type Frame = Schemas["FrameOut"];
export type FixStage = FixSummary["stage"];
export type FixStatus = FixSummary["status"];
export type FixSource = FixSummary["source"];
export type FixRisk = NonNullable<FixSummary["risk"]>;

/** The stages in order, as the run page's rail shows them (approval is a stop, not a step). */
export const STAGES: readonly FixStage[] = [
  "intake",
  "investigate",
  "reproduce",
  "root_cause",
  "plan",
  "awaiting_approval",
  "patch",
  "verify",
  "pr",
  "done",
];

export const STAGE_LABELS: Record<FixStage, string> = {
  intake: "Intake",
  investigate: "Investigate",
  reproduce: "Reproduce",
  root_cause: "Root cause",
  plan: "Plan",
  awaiting_approval: "Approve",
  patch: "Patch",
  verify: "Verify",
  pr: "PR",
  done: "Done",
};

export const STATUS_LABELS: Record<FixStatus, string> = {
  needs_triage: "Needs triage",
  queued: "Queued",
  running: "Running",
  awaiting_approval: "Awaiting approval",
  succeeded: "PR opened",
  not_reproduced: "Not reproduced",
  blocked: "Blocked",
  failed: "Failed",
  rejected: "Rejected",
  cancelled: "Cancelled",
};

/** The statuses the list can be filtered by, in the order people look for them. */
export const STATUS_FILTERS: readonly FixStatus[] = [
  "awaiting_approval",
  "running",
  "queued",
  "needs_triage",
  "succeeded",
  "not_reproduced",
  "blocked",
  "failed",
  "rejected",
  "cancelled",
];

export const SOURCE_LABELS: Record<FixSource, string> = {
  paste: "Pasted trace",
  github_issue: "GitHub issue",
  sentry: "Sentry",
  request: "Request",
};

export const RISK_LABELS: Record<FixRisk, string> = {
  low: "Low risk",
  medium: "Medium risk",
  high: "High risk",
};

export type Tone = "ok" | "warn" | "danger" | "info" | "neutral";

/** How a status reads at a glance: going, waiting on people, done, or stopped short. */
export function statusTone(status: FixStatus): Tone {
  switch (status) {
    case "succeeded":
      return "ok";
    case "awaiting_approval":
    case "needs_triage":
      return "warn";
    case "queued":
    case "running":
      return "info";
    case "failed":
    case "blocked":
      return "danger";
    case "not_reproduced":
    case "rejected":
    case "cancelled":
      return "neutral";
  }
}

export function riskTone(risk: FixRisk): Tone {
  return risk === "high" ? "danger" : risk === "medium" ? "warn" : "ok";
}

/** Still going (it can be cancelled), as opposed to finished one way or another. */
export function isOpen(status: FixStatus): boolean {
  return ["needs_triage", "queued", "running", "awaiting_approval"].includes(status);
}

/** Where a frame opens in the code viewer: its file at its line (null: not the repository's). */
export function frameHref(org: string, repo: string, frame: Frame): Route | null {
  if (!frame.in_repo || !frame.path) return null;
  return codePath(org, repo, frame.path, { lines: String(frame.line) });
}

/** A frame as a chip reads it: the repository's path (or the trace's file) and line. */
export function frameLabel(frame: Frame): string {
  return `${frame.path ?? frame.file}:${String(frame.line)}`;
}

/** A GitHub issue as people give it: a number, `#number` or the issue's URL. */
export function looksLikeIssue(given: string): boolean {
  const text = given.trim();
  return /^#?\d+$/.test(text) || /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/issues\/\d+/.test(text);
}

// ---------------------------------------------------------------- what a fix found and did

export interface Lines {
  path: string;
  start_line: number;
  end_line: number;
}

export interface Hypothesis {
  claim: string;
  likelihood: number;
  evidence: Lines[];
}

export interface RootCause extends Lines {
  claim: string;
  confidence: "high" | "medium" | "low" | null;
  suspect_commit: string | null;
  hypotheses: Hypothesis[];
  seen_in_repro: boolean | null;
}

export interface TestRun {
  passed: boolean;
  exit_code: number | null;
  output: string;
  for_reason?: boolean;
  timed_out?: boolean;
}

export interface Repro {
  framework: string | null;
  test_path: string | null;
  test: string | null;
  explanation: string;
  tries: TestRun[];
  runs: TestRun[];
}

export interface Finding {
  rule: string;
  path: string;
  detail: string;
}

export interface Attempt {
  n: number;
  passed: boolean;
  blocked: boolean;
  reason: string;
  output: string;
}

export interface Patch {
  diff: string;
  files: string[];
  lines: number;
  summary: string;
  attempts: Attempt[];
  findings: Finding[];
}

export interface Verification {
  repro_runs: TestRun[];
  suite: { ok: boolean; verdict: string } | null;
  verdict: { test_shaped: boolean; reason: string; concerns: string[] } | null;
  readiness: { score: number; reason: string } | null;
  unverified: string[];
  findings: Finding[];
  risk: { level: FixRisk; reasons: string[] } | null;
}

function obj(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function arr<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function rootCauseOf(fix: Pick<Fix, "root_cause">): RootCause | null {
  const raw = obj(fix.root_cause);
  if (!raw?.path) return null;
  return {
    claim: str(raw.claim),
    path: str(raw.path),
    start_line: Number(raw.start_line ?? 1),
    end_line: Number(raw.end_line ?? raw.start_line ?? 1),
    confidence: (raw.confidence as RootCause["confidence"] | undefined) ?? null,
    suspect_commit: typeof raw.suspect_commit === "string" ? raw.suspect_commit : null,
    hypotheses: arr<Hypothesis>(raw.hypotheses),
    seen_in_repro: typeof raw.seen_in_repro === "boolean" ? raw.seen_in_repro : null,
  };
}

export function reproOf(fix: Pick<Fix, "repro">): Repro | null {
  const raw = obj(fix.repro);
  if (!raw) return null;
  return {
    framework: typeof raw.framework === "string" ? raw.framework : null,
    test_path: typeof raw.test_path === "string" ? raw.test_path : null,
    test: typeof raw.test === "string" ? raw.test : null,
    explanation: str(raw.explanation),
    tries: arr<TestRun>(raw.tries),
    runs: arr<TestRun>(raw.runs),
  };
}

export function patchOf(fix: Pick<Fix, "patch">): Patch | null {
  const raw = obj(fix.patch);
  if (!raw) return null;
  return {
    diff: str(raw.diff),
    files: arr<string>(raw.files),
    lines: Number(raw.lines ?? 0),
    summary: str(raw.summary),
    attempts: arr<Attempt>(raw.attempts),
    findings: arr<Finding>(raw.findings),
  };
}

export function verificationOf(fix: Pick<Fix, "verification">): Verification | null {
  const raw = obj(fix.verification);
  if (!raw) return null;
  const suite = obj(raw.suite);
  return {
    repro_runs: arr<TestRun>(raw.repro_runs),
    suite: suite ? { ok: Boolean(suite.ok), verdict: str(suite.verdict) } : null,
    verdict: (obj(raw.verdict) as Verification["verdict"]) ?? null,
    readiness: (obj(raw.readiness) as Verification["readiness"]) ?? null,
    unverified: arr<string>(raw.unverified),
    findings: arr<Finding>(raw.findings),
    risk: (obj(raw.risk) as Verification["risk"]) ?? null,
  };
}

/** A fix's plan (its steps and files), as its approval card shows and edits it. */
export interface FixPlan {
  summary: string;
  steps: { description: string; path: string }[];
  files: { path: string; change: "modify" | "create"; why: string }[];
  risks: string[];
  risk: { level: FixRisk; reasons: string[] } | null;
  revision: number;
}

export function fixPlanOf(fix: Pick<Fix, "plan">): FixPlan | null {
  const raw = obj(fix.plan);
  if (!raw) return null;
  return {
    summary: str(raw.summary),
    steps: arr<FixPlan["steps"][number]>(raw.steps),
    files: arr<FixPlan["files"][number]>(raw.files),
    risks: arr<string>(raw.risks),
    risk: (obj(raw.risk) as FixPlan["risk"]) ?? null,
    revision: typeof raw.revision === "number" ? raw.revision : 0,
  };
}

/** What each anti-gaming rule means, for people (8.7). */
export const RULE_LABELS: Record<string, string> = {
  test_changed: "An existing test was changed",
  test_missing: "A planned test wasn't written",
  repro_changed: "The reproducing test was changed",
  test_config: "The test setup was changed",
  skip_added: "A test was skipped or narrowed",
  test_detection: "The code checks whether a test is running",
  special_cased: "The code special-cases the test's values",
  assert_removed: "An assertion was removed",
  broad_except: "An error is caught instead of fixed",
  exit_forced: "The code ends the test process",
  fake_report: "The code writes a fake test report",
  shadowing: "A file stands in for a module",
  sensitive_file: "CI, git or build configuration was changed",
  outside_plan: "A file outside the approved plan was changed",
  unreviewable: "A change that can't be read (binary, link, mode)",
};

/** A run's pull request as a link: only a GitHub pull request's address (anything else stored
 * there isn't followed). */
export function prLink(url: string | null): string | null {
  return url && /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/\d+$/.test(url) ? url : null;
}
