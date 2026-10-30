"use client";

/**
 * One step of a run (4.12), the centre of its timeline: what it was (a model turn, a tool
 * call and its input, a retrieval, a check), what it took (ms, tokens), what it found (its
 * summary, and its output once the run's detail has it), and the files it read (each opens in
 * the code browser).
 */
import { FileCode2 } from "lucide-react";
import Link from "next/link";

import { useRepoSlug } from "@/components/repo/use-repo";
import { filesOf, formatMs, stepTitle, type Step } from "@/lib/api/runs";
import { codePath } from "@/lib/routes";

export function StepCard({ step }: { step: Step }) {
  const { org, repo } = useRepoSlug();
  const files = filesOf(step);
  const output: unknown = step.output;
  return (
    <article className="flex flex-col gap-4" data-testid="step-card" aria-label="Step">
      <header className="flex flex-col gap-1">
        <p className="text-xs text-fg-muted">
          Step {step.idx + 1} · {step.kind}
          {step.kind !== "llm" ? ` · ${step.name}` : ""}
        </p>
        <h2 className="text-base font-semibold">{stepTitle(step)}</h2>
        <dl className="flex flex-wrap gap-4 font-mono text-xs text-fg-muted">
          <div>
            <dt className="sr-only">Took</dt>
            <dd>{formatMs(step.ms)}</dd>
          </div>
          <div>
            <dt className="sr-only">Started at</dt>
            <dd>at {formatMs(step.offset_ms)}</dd>
          </div>
          {step.tokens ? (
            <div>
              <dt className="sr-only">Tokens</dt>
              <dd>{step.tokens.toLocaleString()} tokens</dd>
            </div>
          ) : null}
          {step.kind === "llm" ? (
            <div>
              <dt className="sr-only">Model</dt>
              <dd>{step.name}</dd>
            </div>
          ) : null}
        </dl>
      </header>
      {step.summary ? <p className="text-sm">{step.summary}</p> : null}
      {files.length ? (
        <section aria-label="Files it read" className="flex flex-col gap-1">
          <h3 className="text-xs font-medium text-fg-muted">Files</h3>
          <ul className="flex flex-col gap-1">
            {files.map((path) => (
              <li key={path}>
                <Link
                  href={codePath(org, repo, path)}
                  className="flex items-center gap-1.5 font-mono text-xs hover:underline"
                >
                  <FileCode2 aria-hidden className="size-3.5 text-fg-muted" />
                  {path}
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <section aria-label="Input" className="flex flex-col gap-1">
        <h3 className="text-xs font-medium text-fg-muted">Input</h3>
        <pre className="max-h-48 overflow-auto rounded-sm bg-surface-2 p-2 font-mono text-[11px]">
          {JSON.stringify(step.input, null, 2)}
        </pre>
      </section>
      {step.has_output ? (
        <section aria-label="Output" className="flex flex-col gap-1">
          <h3 className="text-xs font-medium text-fg-muted">Output</h3>
          <pre className="max-h-72 overflow-auto rounded-sm bg-surface-2 p-2 font-mono text-[11px]">
            {output === undefined
              ? "Arrives once the run has finished."
              : output === null
                ? "Kept in the run's store (too big to show here)."
                : JSON.stringify(output, null, 2)}
          </pre>
        </section>
      ) : null}
    </article>
  );
}
