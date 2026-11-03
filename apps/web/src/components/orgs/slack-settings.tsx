"use client";

/**
 * `/[org]/settings/integrations` (8.10, 10.7): the org's Slack. An admin connects an incoming
 * webhook (Slack's own: `https://hooks.slack.com/services/…`) and picks what it posts; the
 * address is kept encrypted and never shown again but for its last characters. Anyone with
 * the channel open reads what's posted, whatever repositories they can read on GitHub: that's
 * the admins' choice, said beside it.
 */
import { useQueryClient } from "@tanstack/react-query";
import { Hash, Unplug } from "lucide-react";
import Link from "next/link";
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
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { NotificationKind } from "@/lib/api/inbox";
import type { Schemas } from "@/lib/api/types";

type Slack = Schemas["SlackOut"];

const PATH = "/api/v1/orgs/{org}/integrations/slack" as const;
const WEBHOOK = /^https:\/\/hooks\.slack\.com\/services\/[A-Za-z0-9_-]+(\/[A-Za-z0-9_-]+){1,3}$/;

/** What it may post, in the words the settings use (the same kinds as a person's inbox). */
const KINDS: { kind: NotificationKind; label: string }[] = [
  { kind: "approval_needed", label: "Plans waiting for approval" },
  { kind: "pr_opened", label: "Pull requests opened" },
  { kind: "fix_failed", label: "Fixes and changes that stopped" },
  { kind: "index_done", label: "Repositories indexed" },
  { kind: "index_failed", label: "Indexing that failed" },
  { kind: "cost_cap", label: "Spend near the daily cap" },
];
const SUGGESTED: NotificationKind[] = ["approval_needed", "pr_opened", "fix_failed"];

export function SlackSettings() {
  const { slug, role } = useOrgRole();
  const admin = roleAtLeast(role, "admin");
  const slack = $api.useQuery(
    "get",
    PATH,
    { params: { path: { org: slug } } },
    { enabled: admin, retry: false },
  );
  if (role !== null && !admin) {
    return (
      <Section>
        <p className="text-sm text-fg-muted" data-testid="slack-admins-only">
          The organization&apos;s admins connect Slack. What you&apos;re told yourself is in{" "}
          <Link href="/settings/notifications" className="underline">
            your notification settings
          </Link>
          .
        </p>
      </Section>
    );
  }
  if (role === null || slack.isPending) {
    return (
      <Section>
        <div role="status" aria-label="Loading Slack" className="flex flex-col gap-2">
          <Skeleton className="h-control-md" />
          <Skeleton className="h-control-md" />
        </div>
      </Section>
    );
  }
  const missing = slack.error instanceof ApiError && slack.error.code === "slack_not_connected";
  if (slack.isError && !missing) {
    return (
      <Section>
        <ErrorState title="Couldn't load Slack" onRetry={() => void slack.refetch()} />
      </Section>
    );
  }
  // Keyed on what's saved, so the form starts again from it after each save.
  // A query that fails keeps what it last had: "not connected" is the answer now.
  const saved = missing ? null : (slack.data ?? null);
  return (
    <Section>
      <SlackForm
        key={saved ? `${saved.channel}:${saved.kinds.join()}:${saved.webhook_hint}` : "new"}
        saved={saved}
      />
    </Section>
  );
}

function Section({ children }: { children: React.ReactNode }) {
  return (
    <section aria-labelledby="slack-heading" className="flex flex-col gap-4">
      <div>
        <h2 id="slack-heading" className="flex items-center gap-1.5 text-lg font-semibold">
          <Hash aria-hidden className="size-4" />
          Slack
        </h2>
        <p className="text-sm text-fg-muted">
          Post what happens here to one of your Slack channels, through an incoming webhook you make
          in Slack.
        </p>
      </div>
      {children}
    </section>
  );
}

function SlackForm({ saved }: { saved: Slack | null }) {
  const id = useId();
  const { slug } = useOrgRole();
  const queryClient = useQueryClient();
  const [webhook, setWebhook] = useState("");
  const [channel, setChannel] = useState(saved?.channel ?? "");
  const [kinds, setKinds] = useState<Set<NotificationKind>>(new Set(saved?.kinds ?? SUGGESTED));
  const [confirming, setConfirming] = useState(false);
  const key = $api.queryOptions("get", PATH, { params: { path: { org: slug } } }).queryKey;
  const refresh = () => queryClient.invalidateQueries({ queryKey: key });
  const save = $api.useMutation("put", PATH, { onSuccess: refresh });
  const remove = $api.useMutation("delete", PATH, { onSuccess: refresh });
  const url = webhook.trim();
  const wrong = url !== "" && !WEBHOOK.test(url);
  const changed =
    url !== "" ||
    channel.trim() !== (saved?.channel ?? "") ||
    [...kinds].sort().join() !== [...(saved?.kinds ?? [])].sort().join();
  const ready = (saved !== null || url !== "") && !wrong && changed && !save.isPending;

  const submit = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!ready) return;
    try {
      await save.mutateAsync({
        params: { path: { org: slug } },
        body: {
          ...(url ? { webhook_url: url } : {}),
          channel: channel.trim(),
          kinds: KINDS.map((k) => k.kind).filter((k) => kinds.has(k)),
        },
      });
      toast.success(saved ? "Slack updated" : "Slack connected");
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : "Slack couldn't be saved.");
    }
  };

  return (
    <form
      onSubmit={(event) => void submit(event)}
      className="flex flex-col gap-4"
      data-testid="slack-form"
    >
      {saved && (
        <p className="text-sm" data-testid="slack-connected">
          Connected{saved.channel ? ` to ${saved.channel}` : ""}, through the webhook ending{" "}
          <span className="font-mono">{saved.webhook_hint}</span>.
        </p>
      )}
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${id}-webhook`}>
          {saved ? "A new webhook URL (leave empty to keep this one)" : "Incoming webhook URL"}
        </Label>
        <Input
          id={`${id}-webhook`}
          type="url"
          autoComplete="off"
          spellCheck={false}
          placeholder="https://hooks.slack.com/services/…"
          value={webhook}
          aria-invalid={wrong || undefined}
          aria-describedby={`${id}-webhook-help`}
          onChange={(event) => {
            setWebhook(event.target.value);
          }}
        />
        <p
          id={`${id}-webhook-help`}
          className={wrong ? "text-xs text-danger" : "text-xs text-fg-muted"}
        >
          {wrong
            ? "That isn't a Slack incoming webhook: it starts https://hooks.slack.com/services/."
            : "Kept encrypted; only its last characters are shown again."}
        </p>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${id}-channel`}>Channel name (to show here)</Label>
        <Input
          id={`${id}-channel`}
          placeholder="#eng-alerts"
          maxLength={100}
          value={channel}
          onChange={(event) => {
            setChannel(event.target.value);
          }}
        />
      </div>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-sm font-medium">What it posts</legend>
        {KINDS.map(({ kind, label }) => (
          <div key={kind} className="flex items-center gap-2">
            <Checkbox
              id={`${id}-${kind}`}
              checked={kinds.has(kind)}
              onCheckedChange={(on: boolean) => {
                const next = new Set(kinds);
                if (on) next.add(kind);
                else next.delete(kind);
                setKinds(next);
              }}
            />
            <label htmlFor={`${id}-${kind}`} className="text-sm">
              {label}
            </label>
          </div>
        ))}
        <p className="text-xs text-fg-muted">
          Everyone in the channel reads what&apos;s posted, about every repository, whatever they
          can see on GitHub.
        </p>
      </fieldset>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" disabled={!ready}>
          {saved ? "Save" : "Connect Slack"}
        </Button>
        {saved && (
          <Button
            type="button"
            variant="ghost"
            disabled={remove.isPending}
            onClick={() => {
              setConfirming(true);
            }}
          >
            <Unplug aria-hidden />
            Disconnect
          </Button>
        )}
      </div>
      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Disconnect Slack?</AlertDialogTitle>
            <AlertDialogDescription>
              Nothing more is posted to{" "}
              {saved && saved.channel !== "" ? saved.channel : "the channel"}, including what&apos;s
              waiting to be sent. Connecting again needs a webhook URL.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={remove.isPending}
              onClick={() => {
                void remove
                  .mutateAsync({ params: { path: { org: slug } } })
                  .then(() => {
                    setConfirming(false);
                    toast.success("Slack disconnected");
                  })
                  .catch((caught: unknown) => {
                    setConfirming(false);
                    toast.error(
                      caught instanceof ApiError
                        ? caught.message
                        : "Slack couldn't be disconnected.",
                    );
                  });
              }}
            >
              Disconnect
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </form>
  );
}
