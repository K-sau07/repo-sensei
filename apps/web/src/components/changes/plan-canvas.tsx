"use client";

/**
 * A change's plan as a canvas (9.3): the files it touches as cards grouped by layer (UI, API,
 * service, data), each with why and new files marked; the tests it adds; the existing code it
 * follows (cited lines, opening the code viewer); its steps, risks and blast radius. While the
 * plan waits for approval a member can edit it in place: a file's reason, remove one, add one,
 * and the same for its tests. Edits are planned again (the planner reads them) before they
 * can be approved; the plan as it stands is approved as it is.
 */
import { FilePlus2, FileCode2, FlaskConical, Plus, Trash2 } from "lucide-react";
import Link from "next/link";
import { useId, useState } from "react";

import { useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  byLayer,
  LAYER_NAMES,
  layerFor,
  type ChangePlan,
  type PlannedFile,
  type PlannedTest,
} from "@/lib/api/changes";
import { codePath } from "@/lib/routes";

import { BlastRadius } from "./blast-radius";

export interface Draft {
  summary: string;
  files: PlannedFile[];
  tests: PlannedTest[];
}

export function draftOf(plan: ChangePlan): Draft {
  return { summary: plan.summary, files: plan.files, tests: plan.tests };
}

export function PlanCanvas({
  plan,
  draft,
  onDraft,
  editable,
}: {
  plan: ChangePlan;
  draft: Draft;
  onDraft: (draft: Draft) => void;
  editable: boolean;
}) {
  const { org, repo } = useRepoSlug();
  const setFile = (path: string, next: PlannedFile | null) => {
    onDraft({
      ...draft,
      files: draft.files.flatMap((f) => (f.path === path ? (next ? [next] : []) : [f])),
    });
  };
  const setTest = (path: string, next: PlannedTest | null) => {
    onDraft({
      ...draft,
      tests: draft.tests.flatMap((t) => (t.path === path ? (next ? [next] : []) : [t])),
    });
  };
  return (
    <div className="flex flex-col gap-6" data-testid="plan-canvas">
      <section aria-labelledby="plan-summary" className="flex flex-col gap-1.5">
        <h2 id="plan-summary" className="text-sm font-medium text-fg-muted">
          Plan (revision {plan.revision})
        </h2>
        {editable ? (
          <Textarea
            aria-label="The plan's summary"
            value={draft.summary}
            rows={2}
            onChange={(event) => {
              onDraft({ ...draft, summary: event.target.value });
            }}
          />
        ) : (
          <p className="text-sm">{plan.summary}</p>
        )}
      </section>

      <section aria-labelledby="plan-files" className="flex flex-col gap-2">
        <h2 id="plan-files" className="text-sm font-medium text-fg-muted">
          Files, by layer
        </h2>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
          {byLayer(draft.files).map(([layer, files]) => (
            <section
              key={layer}
              aria-label={LAYER_NAMES[layer]}
              data-testid="layer"
              data-layer={layer}
              className="flex flex-col gap-2 rounded-md border border-line p-3"
            >
              <h3 className="text-xs font-semibold tracking-wide text-fg-muted uppercase">
                {LAYER_NAMES[layer]}
              </h3>
              {files.map((file) => (
                <FileCard
                  key={file.path}
                  file={file}
                  editable={editable}
                  href={file.change === "modify" ? codePath(org, repo, file.path) : null}
                  onChange={(next) => {
                    setFile(file.path, next);
                  }}
                />
              ))}
            </section>
          ))}
        </div>
        {editable && (
          <AddFile
            taken={new Set([...draft.files.map((f) => f.path), ...draft.tests.map((t) => t.path)])}
            onAdd={(file) => {
              onDraft({ ...draft, files: [...draft.files, file] });
            }}
          />
        )}
      </section>

      <section aria-labelledby="plan-tests" className="flex flex-col gap-2">
        <h2 id="plan-tests" className="text-sm font-medium text-fg-muted">
          Tests it adds
        </h2>
        <ul className="flex flex-col gap-2">
          {draft.tests.map((test) => (
            <li
              key={test.path}
              data-testid="planned-test"
              className="flex flex-col gap-1 rounded-md border border-line p-3"
            >
              <div className="flex items-center gap-2">
                <FlaskConical aria-hidden className="size-3.5 text-fg-muted" />
                <span className="font-mono text-xs">{test.path}</span>
                <span className="text-xs text-signal">New</span>
                {editable && (
                  <Button
                    type="button"
                    size="icon-sm"
                    variant="ghost"
                    className="ml-auto"
                    aria-label={`Remove the test ${test.path}`}
                    onClick={() => {
                      setTest(test.path, null);
                    }}
                  >
                    <Trash2 aria-hidden />
                  </Button>
                )}
              </div>
              {editable ? (
                <Input
                  aria-label={`What ${test.path} checks`}
                  value={test.checks}
                  onChange={(event) => {
                    setTest(test.path, { ...test, checks: event.target.value });
                  }}
                />
              ) : (
                <p className="text-xs text-fg-muted">{test.checks}</p>
              )}
            </li>
          ))}
        </ul>
        {editable && (
          <AddTest
            taken={new Set([...draft.files.map((f) => f.path), ...draft.tests.map((t) => t.path)])}
            onAdd={(test) => {
              onDraft({ ...draft, tests: [...draft.tests, test] });
            }}
          />
        )}
      </section>

      {plan.patterns.length > 0 && (
        <section aria-labelledby="plan-patterns" className="flex flex-col gap-2">
          <h2 id="plan-patterns" className="text-sm font-medium text-fg-muted">
            Patterns it follows
          </h2>
          <ul className="flex flex-col gap-1 text-xs">
            {plan.patterns.map((pattern) => (
              <li key={`${pattern.path}:${String(pattern.start_line)}`} className="flex gap-2">
                <Link
                  href={codePath(org, repo, pattern.path, {
                    lines: `${String(pattern.start_line)}-${String(pattern.end_line)}`,
                  })}
                  className="shrink-0 font-mono hover:underline"
                >
                  {pattern.path}:{pattern.start_line}–{pattern.end_line}
                </Link>
                <span className="text-fg-muted">{pattern.claim}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
        <section aria-labelledby="plan-steps" className="flex flex-col gap-2">
          <h2 id="plan-steps" className="text-sm font-medium text-fg-muted">
            Steps
          </h2>
          <ol className="flex list-decimal flex-col gap-1 pl-5 text-sm">
            {plan.steps.map((step, i) => (
              <li key={`${step.path}:${String(i)}`}>
                {step.description}{" "}
                <span className="font-mono text-xs text-fg-muted">{step.path}</span>
              </li>
            ))}
          </ol>
          {plan.risks.length > 0 && (
            <>
              <h3 className="mt-2 text-xs font-medium text-fg-muted">What could go wrong</h3>
              <ul className="flex list-disc flex-col gap-1 pl-5 text-xs">
                {plan.risks.map((risk) => (
                  <li key={risk}>{risk}</li>
                ))}
              </ul>
            </>
          )}
        </section>
        <section aria-labelledby="plan-blast" className="flex flex-col gap-2">
          <h2 id="plan-blast" className="text-sm font-medium text-fg-muted">
            Blast radius
          </h2>
          {plan.risk && plan.risk.reasons.length > 0 && (
            <p className="text-xs text-fg-muted" data-testid="risk-reasons">
              {plan.risk.reasons.join("; ")}
            </p>
          )}
          <BlastRadius blast={plan.blast_radius} />
        </section>
      </div>
    </div>
  );
}

function FileCard({
  file,
  editable,
  href,
  onChange,
}: {
  file: PlannedFile;
  editable: boolean;
  href: ReturnType<typeof codePath> | null;
  onChange: (file: PlannedFile | null) => void;
}) {
  const Icon = file.change === "create" ? FilePlus2 : FileCode2;
  return (
    <article data-testid="planned-file" className="flex flex-col gap-1 rounded-sm bg-surface-2 p-2">
      <div className="flex items-center gap-1.5">
        <Icon aria-hidden className="size-3.5 shrink-0 text-fg-muted" />
        {href ? (
          <Link href={href} className="truncate font-mono text-xs hover:underline">
            {file.path}
          </Link>
        ) : (
          <span className="truncate font-mono text-xs">{file.path}</span>
        )}
        {file.change === "create" && <span className="text-xs text-signal">New</span>}
        {editable && (
          <Button
            type="button"
            size="icon-sm"
            variant="ghost"
            className="ml-auto"
            aria-label={`Remove ${file.path} from the plan`}
            onClick={() => {
              onChange(null);
            }}
          >
            <Trash2 aria-hidden />
          </Button>
        )}
      </div>
      {editable ? (
        <Textarea
          aria-label={`Why ${file.path} changes`}
          value={file.why}
          rows={2}
          className="text-xs"
          onChange={(event) => {
            onChange({ ...file, why: event.target.value });
          }}
        />
      ) : (
        <p className="text-xs text-fg-muted">{file.why}</p>
      )}
    </article>
  );
}

function AddFile({ taken, onAdd }: { taken: Set<string>; onAdd: (file: PlannedFile) => void }) {
  const id = useId();
  const [path, setPath] = useState("");
  const [why, setWhy] = useState("");
  const [create, setCreate] = useState(false);
  const clean = path.trim().replace(/^\/+/, "");
  const ok = clean !== "" && !taken.has(clean) && why.trim() !== "";
  return (
    <div className="flex flex-wrap items-end gap-2" data-testid="add-file">
      <div className="flex min-w-64 flex-1 flex-col gap-1">
        <Label htmlFor={`${id}-path`} className="text-xs">
          Add a file
        </Label>
        <Input
          id={`${id}-path`}
          value={path}
          placeholder="app/api/limits.py"
          onChange={(event) => {
            setPath(event.target.value);
          }}
        />
      </div>
      <div className="flex min-w-64 flex-1 flex-col gap-1">
        <Label htmlFor={`${id}-why`} className="text-xs">
          Why
        </Label>
        <Input
          id={`${id}-why`}
          value={why}
          onChange={(event) => {
            setWhy(event.target.value);
          }}
        />
      </div>
      <label className="flex items-center gap-1.5 pb-2 text-xs">
        <input
          type="checkbox"
          checked={create}
          onChange={(event) => {
            setCreate(event.target.checked);
          }}
        />
        A new file
      </label>
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={!ok}
        onClick={() => {
          onAdd({ path: clean, change: create ? "create" : "modify", why: why.trim(),
                  layer: layerFor(clean) }); // prettier-ignore
          setPath("");
          setWhy("");
          setCreate(false);
        }}
      >
        <Plus aria-hidden />
        Add
      </Button>
    </div>
  );
}

function AddTest({ taken, onAdd }: { taken: Set<string>; onAdd: (test: PlannedTest) => void }) {
  const id = useId();
  const [path, setPath] = useState("");
  const [checks, setChecks] = useState("");
  const clean = path.trim().replace(/^\/+/, "");
  const ok = clean !== "" && !taken.has(clean) && checks.trim() !== "";
  return (
    <div className="flex flex-wrap items-end gap-2" data-testid="add-test">
      <div className="flex min-w-64 flex-1 flex-col gap-1">
        <Label htmlFor={`${id}-path`} className="text-xs">
          Add a test file
        </Label>
        <Input
          id={`${id}-path`}
          value={path}
          placeholder="tests/test_rate_limit.py"
          onChange={(event) => {
            setPath(event.target.value);
          }}
        />
      </div>
      <div className="flex min-w-64 flex-1 flex-col gap-1">
        <Label htmlFor={`${id}-checks`} className="text-xs">
          What it checks
        </Label>
        <Input
          id={`${id}-checks`}
          value={checks}
          onChange={(event) => {
            setChecks(event.target.value);
          }}
        />
      </div>
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={!ok}
        onClick={() => {
          onAdd({ path: clean, checks: checks.trim(), layer: "test" });
          setPath("");
          setChecks("");
        }}
      >
        <Plus aria-hidden />
        Add
      </Button>
    </div>
  );
}
