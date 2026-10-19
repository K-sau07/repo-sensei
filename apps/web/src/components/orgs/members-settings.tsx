"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
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
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
import type { Invitation, Member, Role } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";

import { InviteDialog } from "./invite-dialog";
import { assignableRoles, ROLE_DESCRIPTIONS, ROLE_LABELS } from "./roles";

const problem = (error: unknown) =>
  error instanceof ApiError ? error.message : "Something went wrong. Please try again.";

/** What the signed-in user may do to one member row (mirrors the API's rules). The last owner
 * can't leave: someone else must be made an owner first. */
export function memberPermissions(
  myRole: Role | null,
  me: string,
  member: Member,
  ownerCount: number,
) {
  const self = member.user_id === me;
  const admin = roleAtLeast(myRole, "admin");
  const touchesOwner = member.role === "owner";
  const allowed = admin && !self && (!touchesOwner || myRole === "owner");
  const lastOwner = self && member.role === "owner" && ownerCount <= 1;
  return { canChangeRole: allowed, canRemove: allowed, canLeave: self && !lastOwner, lastOwner };
}

/** A button that asks before doing something hard to undo. */
function ConfirmButton({
  label,
  srLabel,
  title,
  description,
  confirm,
  disabled,
  onConfirm,
}: {
  label: string;
  srLabel?: string;
  title: string;
  description: string;
  confirm: string;
  disabled: boolean;
  onConfirm: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger render={<Button variant="outline" size="sm" disabled={disabled} />}>
        {label}
        {srLabel && <span className="sr-only"> {srLabel}</span>}
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{description}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            variant="destructive"
            onClick={() => {
              setOpen(false);
              onConfirm();
            }}
          >
            {confirm}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function MemberRow({
  org,
  member,
  myRole,
  myId,
  ownerCount,
}: {
  org: string;
  member: Member;
  myRole: Role | null;
  myId: string;
  ownerCount: number;
}) {
  const queryClient = useQueryClient();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [pendingOwner, setPendingOwner] = useState(false);
  const { canChangeRole, canRemove, canLeave, lastOwner } = memberPermissions(
    myRole,
    myId,
    member,
    ownerCount,
  );
  const name = member.name ?? member.login;
  const refresh = () => queryClient.invalidateQueries({ queryKey: queryKeys.members(org) });

  const run = async (action: () => Promise<unknown>, success: string) => {
    setBusy(true);
    try {
      await action();
      toast.success(success);
      await refresh();
    } catch (error) {
      toast.error(problem(error));
      // Our own role may have changed elsewhere: refresh it so the controls match.
      if (error instanceof ApiError && (error.status === 403 || error.status === 404)) {
        await Promise.all([refresh(), queryClient.invalidateQueries({ queryKey: queryKeys.me() })]);
      }
    } finally {
      setBusy(false);
    }
  };

  const changeRole = (role: Role) =>
    run(
      () =>
        api.PATCH("/api/v1/orgs/{org}/members/{user_id}", {
          params: { path: { org, user_id: member.user_id } },
          body: { role },
        }),
      `${name} is now ${ROLE_LABELS[role].toLowerCase()}`,
    );

  const remove = () =>
    run(
      () =>
        api.DELETE("/api/v1/orgs/{org}/members/{user_id}", {
          params: { path: { org, user_id: member.user_id } },
        }),
      `Removed ${name}`,
    );

  const leave = async () => {
    setBusy(true);
    try {
      await api.POST("/api/v1/orgs/{org}/leave", { params: { path: { org } } });
      await queryClient.invalidateQueries({ queryKey: queryKeys.me() });
      router.replace("/auth/callback");
    } catch (error) {
      toast.error(problem(error));
      setBusy(false);
    }
  };

  const roles = myRole ? assignableRoles(myRole) : [];
  return (
    <TableRow>
      <TableCell>
        <div className="flex items-center gap-3">
          <Avatar className="size-7" aria-hidden>
            <AvatarFallback className="text-xs">{name.charAt(0).toUpperCase()}</AvatarFallback>
          </Avatar>
          <div className="flex flex-col">
            <span className="flex items-center gap-2 font-medium">
              {name}
              {member.user_id === myId && <Badge variant="secondary">You</Badge>}
            </span>
            <span className="text-xs text-fg-muted">@{member.login}</span>
          </div>
        </div>
      </TableCell>
      <TableCell>
        {canChangeRole ? (
          <Select
            items={roles.map((value) => ({ value, label: ROLE_LABELS[value] }))}
            value={member.role}
            disabled={busy}
            onValueChange={(value) => {
              if (!value || value === member.role) return;
              // Owners can do everything, including removing other owners: ask first.
              if (value === "owner") setPendingOwner(true);
              else void changeRole(value);
            }}
          >
            <SelectTrigger size="sm" className="w-28" aria-label={`Role of ${name}`}>
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
        ) : (
          <span title={ROLE_DESCRIPTIONS[member.role]}>{ROLE_LABELS[member.role]}</span>
        )}
      </TableCell>
      <TableCell className="text-fg-muted">
        <RelativeTime date={member.joined_at} />
      </TableCell>
      <TableCell className="text-right">
        {canLeave ? (
          <ConfirmButton
            label="Leave"
            title={`Leave ${org}?`}
            description="You'll lose access to this organization until someone invites you again."
            confirm="Leave"
            disabled={busy}
            onConfirm={() => void leave()}
          />
        ) : lastOwner ? (
          <span className="text-xs text-fg-muted">Make someone else an owner to leave</span>
        ) : canRemove ? (
          <ConfirmButton
            label="Remove"
            srLabel={name}
            title={`Remove ${name}?`}
            description={`${name} loses access to this organization right away. You can invite them again later.`}
            confirm="Remove"
            disabled={busy}
            onConfirm={() => void remove()}
          />
        ) : null}
        <AlertDialog open={pendingOwner} onOpenChange={setPendingOwner}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Make {name} an owner?</AlertDialogTitle>
              <AlertDialogDescription>
                Owners can do everything, including deleting the organization and removing other
                owners.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                onClick={() => {
                  setPendingOwner(false);
                  void changeRole("owner");
                }}
              >
                Make owner
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </TableCell>
    </TableRow>
  );
}

function PendingInvitations({ org }: { org: string }) {
  const queryClient = useQueryClient();
  const invitations = $api.useQuery("get", "/api/v1/orgs/{org}/invitations", {
    params: { path: { org } },
  });
  const revoke = async (invitation: Invitation) => {
    try {
      await api.DELETE("/api/v1/orgs/{org}/invitations/{invitation_id}", {
        params: { path: { org, invitation_id: invitation.id } },
      });
      toast.success("Invitation revoked");
      await queryClient.invalidateQueries({ queryKey: queryKeys.invitations(org) });
    } catch (error) {
      toast.error(problem(error));
    }
  };
  if (invitations.isPending) return <Skeleton className="h-row" aria-hidden />;
  if (invitations.isError || invitations.data.length === 0) return null;
  return (
    <section aria-labelledby="pending-heading" className="flex flex-col gap-2">
      <h3 id="pending-heading" className="text-sm font-medium">
        Pending invitations
      </h3>
      <p className="text-xs text-fg-muted">
        Invitation links are shown once, when created. To send a new one, revoke and invite again.
      </p>
      <ul className="divide-y divide-line rounded-lg border border-line">
        {invitations.data.map((invitation) => {
          const who = invitation.email ?? `@${invitation.github_login ?? "unknown"}`;
          return (
            <li key={invitation.id} className="flex items-center gap-4 px-4 py-2 text-sm">
              <span className="min-w-0 flex-1 truncate">{who}</span>
              <span className="text-fg-muted">{ROLE_LABELS[invitation.role]}</span>
              <span className="text-xs text-fg-muted">
                expires <RelativeTime date={invitation.expires_at} />
              </span>
              <Button variant="ghost" size="sm" onClick={() => void revoke(invitation)}>
                Revoke<span className="sr-only"> the invitation for {who}</span>
              </Button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function MembersSettings() {
  const { slug, role, me } = useOrgRole();
  const members = $api.useQuery("get", "/api/v1/orgs/{org}/members", {
    params: { path: { org: slug } },
  });
  const isAdmin = roleAtLeast(role, "admin");
  const ownerCount = members.data?.filter((member) => member.role === "owner").length ?? 0;

  if (members.isPending || !me.data) {
    return (
      <div role="status" aria-label="Loading members" className="flex flex-col gap-2">
        <Skeleton className="h-row" />
        <Skeleton className="h-row" />
      </div>
    );
  }
  if (members.isError) {
    return <ErrorState title="Couldn't load members" onRetry={() => void members.refetch()} />;
  }
  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">Members</h2>
          <p className="text-sm text-fg-muted">
            {isAdmin
              ? "Invite people and choose what they can do."
              : `Only admins and owners can invite or change roles. You're a ${role ?? "member"} here.`}
          </p>
        </div>
        {isAdmin && role && <InviteDialog org={slug} myRole={role} />}
      </header>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Person</TableHead>
            <TableHead>Role</TableHead>
            <TableHead>Joined</TableHead>
            <TableHead className="text-right">
              <span className="sr-only">Actions</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {members.data.map((member) => (
            <MemberRow
              key={member.user_id}
              org={slug}
              member={member}
              myRole={role}
              myId={me.data.id}
              ownerCount={ownerCount}
            />
          ))}
        </TableBody>
      </Table>
      {isAdmin && <PendingInvitations org={slug} />}
    </div>
  );
}
