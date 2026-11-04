"use client";

/**
 * A repository's settings (admins): how it is indexed (automatically, its pull requests), the
 * branches tracked besides its default one, what the index leaves out, whether the public
 * gallery shows it (the explore org's, imported by URL: 10.2), and disconnecting it (its
 * index purged), confirmed by typing its name.
 */
import { useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Unplug, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useId, useState, type SubmitEvent } from "react";
import { toast } from "sonner";

import { ErrorState } from "@/components/feedback/error-state";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { Repo } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";
import { explorePath, orgPath } from "@/lib/routes";

import { useRepo, useRepoSlug } from "./use-repo";

const MAX_BRANCHES = 20; // the API's
const PATH = "/api/v1/orgs/{org}/repos/{repo}";

export function RepoSettings() {
  const repo = useRepo();
  const { role, me } = useOrgRole();
  if (repo.isError || me.isError) {
    return (
      <Page>
        <ErrorState
          title="The repository's settings didn't load"
          onRetry={() => {
            void repo.refetch();
            void me.refetch();
          }}
        />
      </Page>
    );
  }
  if (!repo.data || role === null) {
    return (
      <Page>
        <div role="status" aria-label="Loading settings" className="flex flex-col gap-4">
          <Skeleton className="h-24" />
          <Skeleton className="h-40" />
        </div>
      </Page>
    );
  }
  const admin = roleAtLeast(role, "admin");
  return (
    <Page>
      {!admin && (
        <p className="rounded-md border border-border bg-muted px-3 py-2 text-sm text-fg-muted">
          Only the organization&apos;s admins change a repository&apos;s settings.
        </p>
      )}
      {/* Started again from what was saved, whenever that changes. */}
      <IndexingForm key={JSON.stringify(repo.data.settings)} repo={repo.data} admin={admin} />
      {/* Also once published, should it stop being publishable: it can still be taken out. */}
      {(repo.data.publishable || repo.data.published_at) && (
        <Gallery repo={repo.data} admin={admin} />
      )}
      {admin && <Disconnect repo={repo.data} />}
    </Page>
  );
}

function Page({ children }: { children: React.ReactNode }) {
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-8 p-6">
      <h2 className="text-xl font-semibold">Settings</h2>
      {children}
    </div>
  );
}

function useSaved() {
  const { org, repo } = useRepoSlug();
  const queryClient = useQueryClient();
  return async (updated?: Repo) => {
    // Only its settings and publication from the answer: a PATCH's has no index summary.
    if (updated) {
      queryClient.setQueryData(queryKeys.repo(org, repo), (old: Repo | undefined) =>
        old ? { ...old, settings: updated.settings, published_at: updated.published_at } : old,
      );
    }
    await queryClient.invalidateQueries({ queryKey: queryKeys.repo(org, repo) });
    await queryClient.invalidateQueries({ queryKey: queryKeys.repos(org) });
  };
}

function IndexingForm({ repo, admin }: { repo: Repo; admin: boolean }) {
  const id = useId();
  const { org, repo: slug } = useRepoSlug();
  const saved = useSaved();
  const now = repo.settings;
  const [auto, setAuto] = useState(now.auto_index);
  const [pulls, setPulls] = useState(now.index_pull_requests);
  const [branches, setBranches] = useState<string[]>(now.tracked_branches ?? []);
  const [branch, setBranch] = useState("");
  const [globs, setGlobs] = useState((now.ignore_globs ?? []).join("\n"));
  const [problems, setProblems] = useState<string[]>([]);
  const save = $api.useMutation("patch", PATH);

  // As the API keeps them: trimmed, without blanks or repeats.
  const lines = [
    ...new Set(
      globs
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean),
    ),
  ];
  const changed =
    auto !== now.auto_index ||
    pulls !== now.index_pull_requests ||
    branches.join("\n") !== (now.tracked_branches ?? []).join("\n") ||
    lines.join("\n") !== (now.ignore_globs ?? []).join("\n");

  const add = () => {
    const name = branch.trim();
    if (!name || name === repo.default_branch || branches.includes(name)) {
      setBranch("");
      return;
    }
    if (branches.length >= MAX_BRANCHES) return;
    setBranches([...branches, name]);
    setBranch("");
  };

  const submit = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!admin || !changed || save.isPending) return;
    setProblems([]);
    try {
      const updated = await save.mutateAsync({
        params: { path: { org, repo: slug } },
        body: {
          auto_index: auto,
          index_pull_requests: pulls,
          tracked_branches: branches,
          ignore_globs: lines,
        },
      });
      await saved(updated);
      toast.success("Settings saved");
    } catch (caught) {
      const details = caught instanceof ApiError ? caught.details : null;
      const said =
        details && typeof details === "object" && "problems" in details ? details.problems : null;
      setProblems(
        Array.isArray(said)
          ? said.map(String)
          : [caught instanceof ApiError ? caught.message : "The settings couldn't be saved."],
      );
    }
  };

  return (
    <form
      onSubmit={(event) => void submit(event)}
      className="flex flex-col gap-6"
      aria-labelledby={`${id}-title`}
    >
      <section className="flex flex-col gap-4">
        <h2 id={`${id}-title`} className="font-medium">
          Indexing
        </h2>
        <Toggle
          id={`${id}-auto`}
          label="Index automatically"
          help="After each push to a tracked branch (only what changed). Off: only when someone starts a run."
          checked={auto}
          onChange={setAuto}
          disabled={!admin}
        />
        <Toggle
          id={`${id}-pulls`}
          label="Index pull requests"
          help="Each open pull request's head, as an overlay on the default branch's index."
          checked={pulls}
          onChange={setPulls}
          disabled={!admin}
        />
      </section>

      <section className="flex flex-col gap-2" aria-labelledby={`${id}-branches`}>
        <h2 id={`${id}-branches`} className="font-medium">
          Tracked branches
        </h2>
        <p className="text-sm text-fg-muted">
          <span className="font-mono">{repo.default_branch}</span> (the default) always is; these
          are indexed besides it.
        </p>
        {branches.length > 0 && (
          <ul className="flex flex-wrap gap-1.5" aria-label="Tracked branches">
            {branches.map((name) => (
              <li
                key={name}
                className="inline-flex items-center gap-1 rounded-sm border border-border px-1.5 py-0.5 font-mono text-xs"
              >
                {name}
                {admin && (
                  <button
                    type="button"
                    aria-label={`Stop tracking ${name}`}
                    className="text-fg-muted hover:text-fg"
                    onClick={() => {
                      setBranches(branches.filter((b) => b !== name));
                    }}
                  >
                    <X aria-hidden className="size-3" />
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
        {admin && (
          <div className="flex gap-2">
            <Label htmlFor={`${id}-branch`} className="sr-only">
              A branch to track
            </Label>
            <Input
              id={`${id}-branch`}
              placeholder="release"
              value={branch}
              spellCheck={false}
              autoComplete="off"
              className="max-w-xs font-mono"
              onChange={(event) => {
                setBranch(event.target.value);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  add();
                }
              }}
            />
            <Button
              type="button"
              variant="outline"
              disabled={!branch.trim() || branches.length >= MAX_BRANCHES}
              onClick={add}
            >
              Track
            </Button>
          </div>
        )}
      </section>

      <section className="flex flex-col gap-2">
        <Label htmlFor={`${id}-globs`} className="font-medium">
          Left out of the index
        </Label>
        <Textarea
          id={`${id}-globs`}
          value={globs}
          rows={5}
          readOnly={!admin}
          spellCheck={false}
          placeholder={"docs/generated/**\n*.min.js"}
          aria-describedby={`${id}-globs-help`}
          className="font-mono text-sm"
          onChange={(event) => {
            setGlobs(event.target.value);
          }}
        />
        <p id={`${id}-globs-help`} className="text-xs text-fg-muted">
          One pattern a line, as in <span className="font-mono">.gitignore</span>; they win over the
          repository&apos;s own <span className="font-mono">.senseiignore</span> and{" "}
          <span className="font-mono">.gitignore</span>. Taken up by the next index.
        </p>
      </section>

      {problems.length > 0 && (
        <div role="alert" className="text-sm text-danger">
          <ul className="flex flex-col gap-1">
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </div>
      )}
      {admin && (
        <div>
          <Button type="submit" disabled={!changed || save.isPending}>
            Save
          </Button>
        </div>
      )}
    </form>
  );
}

function Toggle({
  id,
  label,
  help,
  checked,
  onChange,
  disabled,
}: {
  id: string;
  label: string;
  help: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled: boolean;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="flex flex-col gap-0.5">
        <Label htmlFor={id}>{label}</Label>
        <p id={`${id}-help`} className="text-xs text-fg-muted">
          {help}
        </p>
      </div>
      <Switch
        id={id}
        checked={checked}
        disabled={disabled}
        aria-describedby={`${id}-help`}
        onCheckedChange={(value) => {
          onChange(value);
        }}
      />
    </div>
  );
}

function Gallery({ repo, admin }: { repo: Repo; admin: boolean }) {
  const id = useId();
  const { org, repo: slug } = useRepoSlug();
  const saved = useSaved();
  const publish = $api.useMutation("put", `${PATH}/published`);
  const unpublish = $api.useMutation("delete", `${PATH}/published`);
  const shown = repo.published_at !== null && repo.published_at !== undefined;
  const busy = publish.isPending || unpublish.isPending;

  const flip = async (show: boolean) => {
    const params = { params: { path: { org, repo: slug } } };
    try {
      if (show) await saved(await publish.mutateAsync(params));
      else {
        await unpublish.mutateAsync(params);
        await saved();
      }
      toast.success(show ? "Shown in the public gallery" : "Taken out of the public gallery");
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : "That didn't go through.");
    }
  };

  return (
    <section className="flex flex-col gap-3" aria-labelledby={`${id}-title`}>
      <h2 id={`${id}-title`} className="font-medium">
        Public gallery
      </h2>
      <div className="flex items-start justify-between gap-4">
        <div className="flex flex-col gap-0.5">
          <Label htmlFor={`${id}-shown`}>Show it in the public gallery</Label>
          <p id={`${id}-help`} className="text-xs text-fg-muted">
            Anyone may read its overview and feature maps, and ask about it: their questions are
            billed to this organization, within its daily cap.
          </p>
        </div>
        <Switch
          id={`${id}-shown`}
          checked={shown}
          disabled={!admin || busy}
          aria-describedby={`${id}-help`}
          onCheckedChange={(value) => void flip(value)}
        />
      </div>
      {shown && (
        <Link
          href={explorePath(repo.owner, repo.name)}
          className="inline-flex w-fit items-center gap-1 text-sm text-fg-muted hover:text-fg"
        >
          Its public page <ExternalLink aria-hidden className="size-3.5" />
        </Link>
      )}
    </section>
  );
}

function Disconnect({ repo }: { repo: Repo }) {
  const id = useId();
  const { org, repo: slug } = useRepoSlug();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const remove = $api.useMutation("delete", PATH);
  const confirmed = typed.trim().toLowerCase() === repo.full_name.toLowerCase(); // GitHub's: any case

  const disconnect = async () => {
    if (!confirmed || remove.isPending) return;
    try {
      await remove.mutateAsync({ params: { path: { org, repo: slug } } });
      await queryClient.invalidateQueries({ queryKey: queryKeys.repos(org) });
      toast.success(`${repo.full_name} disconnected`);
      router.push(orgPath(org, "repos"));
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : "It couldn't be disconnected.");
    }
  };

  return (
    <section
      className="flex flex-col gap-3 rounded-lg border border-danger/40 p-4"
      aria-labelledby={`${id}-title`}
    >
      <h2 id={`${id}-title`} className="font-medium">
        Disconnect
      </h2>
      <p className="text-sm text-fg-muted">
        Its index, conversations, diagrams and runs here are deleted. The repository on GitHub
        isn&apos;t touched; it can be connected again later.
      </p>
      <div>
        <Button
          type="button"
          variant="destructive"
          onClick={() => {
            setTyped("");
            setOpen(true);
          }}
        >
          <Unplug aria-hidden />
          Disconnect {repo.full_name}
        </Button>
      </div>
      <AlertDialog open={open} onOpenChange={setOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Disconnect {repo.full_name}?</AlertDialogTitle>
            <AlertDialogDescription>
              Everything repo-sensei keeps of it is deleted. Type its name to go on.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <Label htmlFor={`${id}-name`} className="sr-only">
            The repository&apos;s name
          </Label>
          <Input
            id={`${id}-name`}
            value={typed}
            placeholder={repo.full_name}
            autoComplete="off"
            spellCheck={false}
            className="font-mono"
            onChange={(event) => {
              setTyped(event.target.value);
            }}
          />
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={!confirmed || remove.isPending}
              onClick={() => void disconnect()}
            >
              Disconnect
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
