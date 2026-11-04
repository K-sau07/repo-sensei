import type { Metadata } from "next";

import { RunStages } from "@/components/fixes/run-stages";
import { ProductPage } from "@/components/marketing/product/product-page";

const LEAD =
  "Hand it a stack trace, a GitHub issue or an error Sentry saw. It finds the cause, proves it with a failing test, and proposes a patch you approve, verified by the tests.";

export const metadata: Metadata = {
  title: "Fix",
  description: LEAD,
  alternates: { canonical: "/product/fix" },
};

const POINTS = [
  "Nothing is patched until a test reproduces the bug; a flaky reproduction stops the run.",
  "A person approves the plan before any code changes; a risky one needs two.",
  "Tests run in a sandbox with no network; only installing dependencies reaches the package registries.",
  "A patch counts only when the reproduction passes and no test that passed before fails.",
  "Changes to tests, skipped tests or a faked report are caught and refused.",
  "Changes in plain words go the same way: planned by layer, approved, patched with new tests.",
] as const;

const STEPS: readonly { title: string; body: string }[] = [
  { title: "Intake", body: "The trace, issue or Sentry error is read into frames in your code." },
  { title: "Investigate", body: "An agent reads the code and its history to find the cause." },
  { title: "Reproduce", body: "It writes a test that fails for that reason, and runs it three times." },
  { title: "Root cause", body: "What else the cause reaches, from the code's graph." },
  { title: "Plan", body: "The files and steps of the fix, with its risk." },
  { title: "Approve", body: "People with write access approve it, ask for changes, or reject it." },
  { title: "Patch", body: "The plan is carried out in the sandbox, file by file." },
  { title: "Verify", body: "The whole suite runs again, and the patch is checked for gaming." },
]; // prettier-ignore

export default function FixPage() {
  return (
    <ProductPage title="Fix" lead={LEAD} points={POINTS} example="How a fix runs, stage by stage">
      <div className="flex flex-col gap-5 rounded-xl border border-line bg-surface-1 p-5">
        <RunStages kind="fix" stage="verify" status="running" />
        <ol className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {STEPS.map((step, i) => (
            <li key={step.title} className="flex flex-col gap-1">
              <p className="text-xs text-fg-faint">{i + 1}</p>
              <p className="font-medium">{step.title}</p>
              <p className="text-sm text-fg-muted">{step.body}</p>
            </li>
          ))}
        </ol>
        <p className="text-xs text-fg-muted">
          Opening the pull request on your repository, with the patch, its test and the evidence, is
          coming soon.
        </p>
      </div>
    </ProductPage>
  );
}
