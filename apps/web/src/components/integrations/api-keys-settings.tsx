"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";

import { ErrorState } from "@/components/feedback/error-state";
import { RelativeTime } from "@/components/primitives/relative-time";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { $api, api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { scopeLabel } from "@/lib/api/oauth";
import type { ApiKey } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";
import { orgPath } from "@/lib/routes";

import { CreateKeyDialog } from "./create-key-dialog";

export type KeyState = "active" | "expired" | "revoked";

/** Whether a key still works (`now`: for tests). */
export function keyState(key: ApiKey, now: number = Date.now()): KeyState {
  if (key.revoked_at) return "revoked";
  if (key.expires_at && Date.parse(key.expires_at) <= now) return "expired";
  return "active";
}

/** Working keys, and the rest (revoked or expired), each newest first as the API lists them. */
export function splitKeys(
  keys: readonly ApiKey[],
  now: number = Date.now(),
): { working: ApiKey[]; past: ApiKey[] } {
  const working = keys.filter((key) => keyState(key, now) === "active");
  return { working, past: keys.filter((key) => keyState(key, now) !== "active") };
}

const STATE_LABELS: Record<Exclude<KeyState, "active">, string> = {
  expired: "Expired",
  revoked: "Revoked",
};

function RevokeButton({ org, apiKey }: { org: string; apiKey: ApiKey }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const revoke = async () => {
    setBusy(true);
    try {
      await api.DELETE("/api/v1/orgs/{org}/api-keys/{key_id}", {
        params: { path: { org, key_id: apiKey.id } },
      });
      toast.success(`Revoked ${apiKey.name}`);
      await queryClient.invalidateQueries({ queryKey: queryKeys.apiKeys(org) });
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Couldn't revoke the key.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger render={<Button variant="outline" size="sm" disabled={busy} />}>
        Revoke<span className="sr-only"> {apiKey.name}</span>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Revoke {apiKey.name}?</AlertDialogTitle>
          <AlertDialogDescription>
            It stops working right away, everywhere it&apos;s used. This can&apos;t be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            variant="destructive"
            onClick={() => {
              setOpen(false);
              void revoke();
            }}
          >
            Revoke key
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function KeyRow({ org, apiKey, showOwner }: { org: string; apiKey: ApiKey; showOwner: boolean }) {
  const state = keyState(apiKey);
  const working = state === "active";
  return (
    <TableRow className={working ? undefined : "text-fg-muted"}>
      <TableCell>
        <div className="flex flex-col">
          <span className="flex items-center gap-2 font-medium">
            {apiKey.name}
            {!working && <Badge variant="secondary">{STATE_LABELS[state]}</Badge>}
          </span>
          {showOwner && (
            <span className="text-xs text-fg-muted">
              {apiKey.mine ? "yours" : `@${apiKey.owner_login ?? "unknown"}`}
            </span>
          )}
        </div>
      </TableCell>
      <TableCell>
        <code className="font-mono text-xs">rs_{apiKey.prefix}_…</code>
      </TableCell>
      <TableCell>
        <span className="flex flex-wrap gap-1">
          {apiKey.scopes.map((scope) => (
            <Badge key={scope} variant="outline" title={scopeLabel(scope).detail}>
              {scope}
            </Badge>
          ))}
        </span>
      </TableCell>
      <TableCell className="text-fg-muted">
        {apiKey.last_used_at ? <RelativeTime date={apiKey.last_used_at} /> : "Never"}
      </TableCell>
      <TableCell className="text-fg-muted">
        {apiKey.revoked_at ? (
          "—"
        ) : apiKey.expires_at ? (
          <RelativeTime date={apiKey.expires_at} />
        ) : (
          "Never"
        )}
      </TableCell>
      <TableCell className="text-right">
        {working && <RevokeButton org={org} apiKey={apiKey} />}
      </TableCell>
    </TableRow>
  );
}

function KeyRows({ org, keys, showOwner }: { org: string; keys: ApiKey[]; showOwner: boolean }) {
  return keys.map((apiKey) => (
    <KeyRow key={apiKey.id} org={org} apiKey={apiKey} showOwner={showOwner} />
  ));
}

/** Working keys; revoked and expired ones on request (they only say what was). */
function KeyTable({
  org,
  keys,
  showOwner,
}: {
  org: string;
  keys: readonly ApiKey[];
  showOwner: boolean;
}) {
  const [showPast, setShowPast] = useState(false);
  const { working, past } = splitKeys(keys);
  return (
    <div className="flex flex-col gap-2">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Name</TableHead>
            <TableHead>Key</TableHead>
            <TableHead>Scopes</TableHead>
            <TableHead>Last used</TableHead>
            <TableHead>Expires</TableHead>
            <TableHead className="text-right">
              <span className="sr-only">Actions</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {working.length === 0 && (
            <TableRow>
              <TableCell colSpan={6} className="py-6 text-center text-fg-muted">
                No working keys.
              </TableCell>
            </TableRow>
          )}
          <KeyRows org={org} keys={working} showOwner={showOwner} />
          {showPast && <KeyRows org={org} keys={past} showOwner={showOwner} />}
        </TableBody>
      </Table>
      {past.length > 0 && (
        <Button
          variant="ghost"
          size="sm"
          className="self-start"
          aria-expanded={showPast}
          onClick={() => {
            setShowPast((now) => !now);
          }}
        >
          {showPast
            ? "Hide revoked and expired keys"
            : `Show ${String(past.length)} revoked or expired ${past.length === 1 ? "key" : "keys"}`}
        </Button>
      )}
    </div>
  );
}

export function ApiKeysSettings() {
  const { slug, role } = useOrgRole();
  const keys = $api.useQuery("get", "/api/v1/orgs/{org}/api-keys", {
    params: { path: { org: slug } },
  });
  const isAdmin = roleAtLeast(role, "admin");

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="max-w-prose">
          <h2 className="text-lg font-semibold">API keys</h2>
          <p className="text-sm text-fg-muted">
            For scripts, CI and a local MCP server. A key acts as you in this organization, with the
            scopes you give it, and stops working if you leave.{" "}
            <Link href={orgPath(slug, "settings/mcp")} className="underline underline-offset-2">
              Connect an editor
            </Link>{" "}
            instead to sign in without a key.
          </p>
        </div>
        <CreateKeyDialog org={slug} />
      </header>
      {keys.isPending ? (
        <div role="status" aria-label="Loading API keys" className="flex flex-col gap-2">
          <Skeleton className="h-row" />
          <Skeleton className="h-row" />
        </div>
      ) : keys.isError ? (
        <ErrorState title="Couldn't load API keys" onRetry={() => void keys.refetch()} />
      ) : keys.data.keys.length === 0 ? (
        <p className="rounded-lg border border-dashed border-line px-4 py-8 text-center text-sm text-fg-muted">
          No API keys yet.
        </p>
      ) : (
        <KeyTable org={slug} keys={keys.data.keys} showOwner={isAdmin} />
      )}
      {isAdmin && (
        <p className="text-xs text-fg-muted">
          As an admin you see everyone&apos;s keys here, and can revoke any of them.
        </p>
      )}
    </div>
  );
}
