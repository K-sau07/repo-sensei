"use client";

/**
 * Sentry (admins, 8.3 / 10.7.2): the org's internal integration. Its token and client secret
 * are stored encrypted and never shown again (left empty when changing it: the stored ones are
 * kept), which Sentry projects are which repositories, whether busy issues start a fix by
 * themselves, and the webhook address to give Sentry.
 */
import { useQueryClient } from "@tanstack/react-query";
import { Bug, Plus, X } from "lucide-react";
import { useId, useState, type SubmitEvent } from "react";
import { toast } from "sonner";

import { ErrorState } from "@/components/feedback/error-state";
import { CopyButton } from "@/components/primitives/copy-button";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { Schemas } from "@/lib/api/types";

type Sentry = Schemas["SentryOut"];

const PATH = "/api/v1/orgs/{org}/integrations/sentry";
const PROJECT = /^[a-z0-9][a-z0-9_-]*$/; // a Sentry project slug, as the API takes it
const ORG = /^[a-z0-9][a-z0-9_-]*$/;

export function SentrySettings() {
  const { slug, role } = useOrgRole();
  const admin = roleAtLeast(role, "admin");
  // Kept here: the form starts again from each answer the API gives (its key), and a dialog
  // opened just as one arrives must stay open.
  const [confirming, setConfirming] = useState(false);
  const sentry = $api.useQuery(
    "get",
    PATH,
    { params: { path: { org: slug } } },
    { enabled: admin, retry: false },
  );
  if (role !== null && !admin) {
    return (
      <Section>
        <p className="text-sm text-fg-muted">The organization&apos;s admins connect Sentry.</p>
      </Section>
    );
  }
  if (role === null || sentry.isPending) {
    return (
      <Section>
        <Skeleton className="h-control-md" role="status" aria-label="Loading Sentry" />
      </Section>
    );
  }
  const missing = sentry.error instanceof ApiError && sentry.error.code === "sentry_not_connected";
  if (sentry.isError && !missing) {
    return (
      <Section>
        <ErrorState title="Couldn't load Sentry" onRetry={() => void sentry.refetch()} />
      </Section>
    );
  }
  const saved = missing ? null : (sentry.data ?? null);
  return (
    <Section>
      <SentryForm
        key={saved ? JSON.stringify(saved) : "new"}
        saved={saved}
        confirming={confirming}
        setConfirming={setConfirming}
      />
    </Section>
  );
}

function Section({ children }: { children: React.ReactNode }) {
  return (
    <section aria-labelledby="sentry-heading" className="flex flex-col gap-4">
      <div>
        <h2 id="sentry-heading" className="flex items-center gap-1.5 text-lg font-semibold">
          <Bug aria-hidden className="size-4" />
          Sentry
        </h2>
        <p className="text-sm text-fg-muted">
          Errors Sentry sees become fixes here, through an internal integration you make in Sentry.
        </p>
      </div>
      {children}
    </section>
  );
}

interface Row {
  project: string;
  repo: string;
}

function SentryForm({
  saved,
  confirming,
  setConfirming,
}: {
  saved: Sentry | null;
  /** Whether Disconnect is asking to be sure. */
  confirming: boolean;
  setConfirming: (open: boolean) => void;
}) {
  const id = useId();
  const { slug } = useOrgRole();
  const queryClient = useQueryClient();
  const repos = $api.useQuery("get", "/api/v1/orgs/{org}/repos", {
    params: { path: { org: slug } },
  });
  const [org, setOrg] = useState(saved?.sentry_org ?? "");
  const [apiUrl, setApiUrl] = useState(saved?.api_url ?? "https://sentry.io");
  const [token, setToken] = useState("");
  const [secret, setSecret] = useState("");
  const [rows, setRows] = useState<Row[]>(
    Object.entries(saved?.projects ?? {}).map(([project, repo]) => ({ project, repo })),
  );
  const [autoRun, setAutoRun] = useState(saved?.auto_run ?? false);
  const [minEvents, setMinEvents] = useState(String(saved?.min_events ?? 10));
  const key = $api.queryOptions("get", PATH, { params: { path: { org: slug } } }).queryKey;
  const save = $api.useMutation("put", PATH);
  const remove = $api.useMutation("delete", PATH);

  const projects = rows.filter((r) => r.project.trim() || r.repo);
  const badProject = projects.find((r) => !PROJECT.test(r.project.trim()) || !r.repo);
  const events = Number(minEvents);
  const problems = [
    !ORG.test(org.trim()) && "The Sentry organization is its slug: lowercase, digits, - and _.",
    !/^https:\/\/[^\s/]+/.test(apiUrl.trim()) && "Sentry's address starts https://.",
    saved === null &&
      (token.length < 8 || secret.length < 8) &&
      "Connecting needs the integration's token and client secret.",
    badProject && "Each project needs its slug and a repository.",
    new Set(projects.map((r) => r.project.trim())).size !== projects.length &&
      "A project is listed twice.",
    (!Number.isInteger(events) || events < 1 || events > 100_000) &&
      "The events before a fix starts are a whole number from 1 to 100,000.",
  ].filter((p): p is string => typeof p === "string");
  const ready = problems.length === 0 && !save.isPending;

  const submit = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!ready) return;
    try {
      await save.mutateAsync({
        params: { path: { org: slug } },
        body: {
          sentry_org: org.trim(),
          api_url: apiUrl.trim(),
          ...(token ? { token } : {}),
          ...(secret ? { client_secret: secret } : {}),
          projects: Object.fromEntries(projects.map((r) => [r.project.trim(), r.repo])),
          auto_run: autoRun,
          min_events: events,
        },
      });
      toast.success(saved ? "Sentry updated" : "Sentry connected");
      await queryClient.invalidateQueries({ queryKey: key });
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : "Sentry couldn't be saved.");
    }
  };

  const disconnect = async () => {
    try {
      await remove.mutateAsync({ params: { path: { org: slug } } });
      toast.success("Sentry disconnected");
      setConfirming(false);
      await queryClient.invalidateQueries({ queryKey: key });
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : "Sentry couldn't be disconnected.");
    }
  };

  return (
    <form
      onSubmit={(event) => void submit(event)}
      className="flex flex-col gap-4"
      data-testid="sentry-form"
    >
      {saved && (
        <div className="flex flex-col gap-1.5 rounded-md border border-line p-3 text-sm">
          <p data-testid="sentry-connected">
            Connected to <span className="font-mono">{saved.sentry_org}</span>. In Sentry, send the
            integration&apos;s webhooks to:
          </p>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded-sm bg-muted px-2 py-1 font-mono text-xs">
              {saved.webhook_url}
            </code>
            <CopyButton value={saved.webhook_url} label="Copy the webhook address" />
          </div>
        </div>
      )}
      <div className="grid gap-4 sm:grid-cols-2">
        <Field id={`${id}-org`} label="Sentry organization">
          <Input
            id={`${id}-org`}
            value={org}
            spellCheck={false}
            autoComplete="off"
            placeholder="acme"
            onChange={(e) => {
              setOrg(e.target.value);
            }}
          />
        </Field>
        <Field id={`${id}-url`} label="Sentry's address">
          <Input
            id={`${id}-url`}
            type="url"
            value={apiUrl}
            spellCheck={false}
            onChange={(e) => {
              setApiUrl(e.target.value);
            }}
          />
        </Field>
        <Field id={`${id}-token`} label={saved ? "A new token (empty: keep it)" : "Token"}>
          <Input
            id={`${id}-token`}
            type="password"
            value={token}
            autoComplete="off"
            onChange={(e) => {
              setToken(e.target.value);
            }}
          />
        </Field>
        <Field
          id={`${id}-secret`}
          label={saved ? "A new client secret (empty: keep it)" : "Client secret"}
        >
          <Input
            id={`${id}-secret`}
            type="password"
            value={secret}
            autoComplete="off"
            onChange={(e) => {
              setSecret(e.target.value);
            }}
          />
        </Field>
      </div>

      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-sm font-medium">Which project is which repository</legend>
        {rows.map((row, i) => (
          <div key={i} className="flex items-center gap-2">
            <Label htmlFor={`${id}-project-${i}`} className="sr-only">
              Sentry project {i + 1}
            </Label>
            <Input
              id={`${id}-project-${i}`}
              value={row.project}
              placeholder="backend"
              spellCheck={false}
              className="max-w-48 font-mono"
              onChange={(e) => {
                setRows(rows.map((r, j) => (j === i ? { ...r, project: e.target.value } : r)));
              }}
            />
            <Select
              value={row.repo}
              onValueChange={(value) => {
                setRows(rows.map((r, j) => (j === i ? { ...r, repo: value ?? "" } : r)));
              }}
            >
              <SelectTrigger aria-label={`Repository for project ${i + 1}`} className="w-56">
                <SelectValue>{row.repo || "Choose a repository"}</SelectValue>
              </SelectTrigger>
              <SelectContent>
                {(repos.data ?? []).map((repo) => (
                  <SelectItem key={repo.id} value={repo.slug}>
                    {repo.full_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label={`Remove project ${i + 1}`}
              onClick={() => {
                setRows(rows.filter((_, j) => j !== i));
              }}
            >
              <X aria-hidden />
            </Button>
          </div>
        ))}
        <div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              setRows([...rows, { project: "", repo: "" }]);
            }}
          >
            <Plus aria-hidden />
            Add a project
          </Button>
        </div>
      </fieldset>

      <div className="flex flex-wrap items-center gap-4">
        <div className="flex items-center gap-2">
          <Switch
            id={`${id}-auto`}
            checked={autoRun}
            onCheckedChange={(value) => {
              setAutoRun(value);
            }}
          />
          <Label htmlFor={`${id}-auto`}>Busy issues start a fix by themselves</Label>
        </div>
        <div className="flex items-center gap-2">
          <Label htmlFor={`${id}-events`} className="text-sm text-fg-muted">
            at
          </Label>
          <Input
            id={`${id}-events`}
            type="number"
            min={1}
            max={100_000}
            value={minEvents}
            disabled={!autoRun}
            className="w-24"
            onChange={(e) => {
              setMinEvents(e.target.value);
            }}
          />
          <span className="text-sm text-fg-muted">events in 14 days</span>
        </div>
      </div>

      {problems.length > 0 && (org || token || rows.length > 0) && (
        <ul className="flex flex-col gap-1 text-xs text-fg-muted">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={!ready}>
          {saved ? "Save" : "Connect Sentry"}
        </Button>
        {saved && (
          <Button
            type="button"
            variant="ghost"
            disabled={remove.isPending || save.isPending}
            onClick={() => {
              setConfirming(true);
            }}
          >
            Disconnect Sentry
          </Button>
        )}
      </div>
      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Disconnect Sentry?</AlertDialogTitle>
            <AlertDialogDescription>
              Its token and secret are deleted, and its webhooks are refused from now on. Fixes
              already made from it stay.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={remove.isPending}
              onClick={() => void disconnect()}
            >
              Disconnect
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </form>
  );
}

function Field({ id, label, children }: { id: string; label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
    </div>
  );
}
