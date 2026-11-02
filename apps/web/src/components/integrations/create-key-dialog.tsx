"use client";

import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, TriangleAlert } from "lucide-react";
import { useState, type SubmitEvent } from "react";

import { CopyButton } from "@/components/primitives/copy-button";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { scopeLabel } from "@/lib/api/oauth";
import { queryKeys } from "@/lib/query/keys";

/** What a key can be given (the API's grantable scopes). */
export const KEY_SCOPES = ["read:repo", "ask"] as const;
type KeyScope = (typeof KEY_SCOPES)[number];

const EXPIRY = [
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "365", label: "1 year" },
  { value: "never", label: "Never" },
] as const;
type Expiry = (typeof EXPIRY)[number]["value"];

export function CreateKeyDialog({ org }: { org: string }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<KeyScope[]>(["read:repo"]);
  const [expiry, setExpiry] = useState<Expiry>("90");
  const [key, setKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const ready = name.trim().length > 0 && scopes.length > 0;

  const reset = () => {
    setName("");
    setScopes(["read:repo"]);
    setExpiry("90");
    setKey(null);
    setError(null);
  };

  const toggle = (scope: KeyScope, on: boolean) => {
    setScopes((now) =>
      on
        ? KEY_SCOPES.filter((s) => s === scope || now.includes(s))
        : now.filter((s) => s !== scope),
    );
  };

  const create = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!ready) return;
    setSaving(true);
    setError(null);
    try {
      const { data } = await api.POST("/api/v1/orgs/{org}/api-keys", {
        params: { path: { org } },
        body: {
          name: name.trim(),
          scopes,
          expires_in_days: expiry === "never" ? null : Number(expiry),
        },
      });
      setKey(data?.key ?? null);
      await queryClient.invalidateQueries({ queryKey: queryKeys.apiKeys(org) });
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Couldn't create the key.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open={open}
      // The key is shown once: a stray click outside mustn't throw it away (Escape still closes).
      disablePointerDismissal={key !== null}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) reset();
      }}
    >
      <DialogTrigger render={<Button />}>
        <KeyRound data-icon="inline-start" aria-hidden />
        Create key
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{key ? "Copy your new key" : "Create an API key"}</DialogTitle>
          <DialogDescription>
            {key
              ? "Use it as SENSEI_API_KEY for the sensei command line or a local MCP server."
              : "It acts as you in this organization, with the scopes you choose."}
          </DialogDescription>
        </DialogHeader>
        {key ? (
          <div className="grid gap-3">
            <div className="flex items-center gap-2 rounded-sm border border-line bg-surface-1 p-2">
              <code className="min-w-0 flex-1 font-mono text-xs break-all" data-testid="new-key">
                {key}
              </code>
              <CopyButton value={key} label="Copy API key" />
            </div>
            <p
              role="alert"
              className="flex items-start gap-2 rounded-sm border border-warn/40 bg-warn/10 p-2 text-sm"
            >
              <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
              <span>
                This is the only time it&apos;s shown. Keep it somewhere safe, like a password
                manager or your CI&apos;s secrets, never in code.
              </span>
            </p>
          </div>
        ) : (
          <form
            id="create-key-form"
            onSubmit={(event) => void create(event)}
            className="grid gap-5"
          >
            <div className="grid gap-2">
              <Label htmlFor="key-name">Name</Label>
              <Input
                id="key-name"
                value={name}
                maxLength={100}
                autoComplete="off"
                placeholder="CI, laptop, Cursor…"
                aria-describedby={error ? "key-error" : undefined}
                onChange={(event) => {
                  setName(event.target.value);
                }}
              />
            </div>
            <fieldset className="grid gap-3">
              <legend className="mb-1 text-sm font-medium">What it can do</legend>
              {KEY_SCOPES.map((scope) => {
                const label = scopeLabel(scope);
                const id = `key-scope-${scope.replace(":", "-")}`;
                return (
                  <div key={scope} className="flex items-start gap-3">
                    <Checkbox
                      id={id}
                      className="mt-0.5"
                      checked={scopes.includes(scope)}
                      onCheckedChange={(on) => {
                        toggle(scope, on);
                      }}
                    />
                    <label htmlFor={id} className="grid gap-0.5 text-sm">
                      <span className="font-medium">{label.title}</span>
                      <span className="text-xs text-fg-muted">{label.detail}</span>
                    </label>
                  </div>
                );
              })}
            </fieldset>
            <div className="grid gap-2">
              <Label htmlFor="key-expiry">Expires</Label>
              <Select
                items={EXPIRY.map((option) => ({ value: option.value, label: option.label }))}
                value={expiry}
                onValueChange={(value) => {
                  if (value) setExpiry(value);
                }}
              >
                <SelectTrigger id="key-expiry" className="w-40">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {EXPIRY.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {error && (
              <p id="key-error" role="alert" className="text-sm text-danger">
                {error}
              </p>
            )}
          </form>
        )}
        <DialogFooter>
          {key ? (
            <DialogClose render={<Button />}>I&apos;ve copied it</DialogClose>
          ) : (
            <>
              <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
              <Button type="submit" form="create-key-form" disabled={!ready || saving}>
                {saving ? "Creating…" : "Create key"}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
