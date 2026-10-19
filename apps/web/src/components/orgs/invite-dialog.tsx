"use client";

import { useQueryClient } from "@tanstack/react-query";
import { UserPlus } from "lucide-react";
import { useState, type SubmitEvent } from "react";

import { CopyButton } from "@/components/primitives/copy-button";
import { Button } from "@/components/ui/button";
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
import type { Role } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";

import { ROLE_LABELS, assignableRoles } from "./roles";

/** "octo@example.com" is an email; "octo" or "@octo" is a GitHub username. */
export function parseInvitee(value: string): { email: string } | { github_login: string } | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  if (trimmed.includes("@") && !trimmed.startsWith("@")) return { email: trimmed };
  const login = trimmed.replace(/^@/, "");
  return /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/.test(login) ? { github_login: login } : null;
}

export function InviteDialog({ org, myRole }: { org: string; myRole: Role }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [invitee, setInvitee] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [link, setLink] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const target = parseInvitee(invitee);
  const roles = assignableRoles(myRole);

  const reset = () => {
    setInvitee("");
    setRole("member");
    setLink(null);
    setError(null);
  };

  const send = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!target) return;
    setSending(true);
    setError(null);
    try {
      const { data } = await api.POST("/api/v1/orgs/{org}/invitations", {
        params: { path: { org } },
        body: { ...target, role },
      });
      setLink(data?.link ?? null);
      await queryClient.invalidateQueries({ queryKey: queryKeys.invitations(org) });
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Couldn't send the invitation.");
    } finally {
      setSending(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) reset();
      }}
    >
      <DialogTrigger render={<Button />}>
        <UserPlus data-icon="inline-start" aria-hidden />
        Invite people
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{link ? "Invitation created" : "Invite to the organization"}</DialogTitle>
          <DialogDescription>
            {link
              ? "Share this link with them. It's shown only now, and works once, for 7 days."
              : "They'll join when they sign in with the matching email or GitHub account."}
          </DialogDescription>
        </DialogHeader>
        {link ? (
          <div className="flex items-center gap-2 rounded-sm border border-line bg-surface-1 p-2">
            <code className="min-w-0 flex-1 truncate font-mono text-xs" data-testid="invite-link">
              {link}
            </code>
            <CopyButton value={link} label="Copy invitation link" />
          </div>
        ) : (
          <form id="invite-form" onSubmit={(event) => void send(event)} className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor="invitee">Email or GitHub username</Label>
              <Input
                id="invitee"
                value={invitee}
                autoComplete="off"
                placeholder="friend@example.com or @friend"
                aria-describedby={error ? "invite-error" : undefined}
                onChange={(event) => {
                  setInvitee(event.target.value);
                }}
              />
            </div>
            <div className="grid gap-2">
              <Label id="invite-role-label">Role</Label>
              <Select
                items={roles.map((value) => ({ value, label: ROLE_LABELS[value] }))}
                value={role}
                onValueChange={(value) => {
                  if (value) setRole(value);
                }}
              >
                <SelectTrigger aria-labelledby="invite-role-label" className="w-40">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {roles.map((value) => (
                    <SelectItem key={value} value={value}>
                      {ROLE_LABELS[value]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {error && (
              <p id="invite-error" role="alert" className="text-sm text-danger">
                {error}
              </p>
            )}
          </form>
        )}
        <DialogFooter>
          {link ? (
            <DialogClose render={<Button />}>Done</DialogClose>
          ) : (
            <>
              <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
              <Button type="submit" form="invite-form" disabled={!target || sending}>
                {sending ? "Sending…" : "Send invitation"}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
