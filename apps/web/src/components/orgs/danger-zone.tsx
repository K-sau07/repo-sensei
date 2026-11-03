"use client";

/**
 * An org's danger zone (owners, F19): handing ownership to another member (and stepping down,
 * if they like), and deleting the org, confirmed by typing its name in URLs. A deleted org is
 * hidden at once, restorable by its owners for 7 days (from their profile), then purged.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useId, useState } from "react";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useOrgRole } from "@/hooks/use-org-role";
import { api, $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";

const GRACE_DAYS = 7; // the API's (sensei_core.orgs.lifecycle.ORG_GRACE)

export function OrgDangerZone() {
  const { slug, org, role, me } = useOrgRole();
  if (me.isError) {
    return <ErrorState title="Couldn't load the organization" onRetry={() => void me.refetch()} />;
  }
  if (org === null || role === null) {
    return (
      <div role="status" aria-label="Loading" className="flex flex-col gap-3">
        <Skeleton className="h-32" />
      </div>
    );
  }
  if (role !== "owner") {
    return (
      <p className="text-sm text-fg-muted">
        Only the organization&apos;s owners hand it on or delete it.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-8">
      <TransferOwnership org={slug} me={me.data?.id ?? ""} />
      <DeleteOrg org={slug} name={org.name} />
    </div>
  );
}

function TransferOwnership({ org, me }: { org: string; me: string }) {
  const id = useId();
  const queryClient = useQueryClient();
  const members = $api.useQuery("get", "/api/v1/orgs/{org}/members", {
    params: { path: { org } },
  });
  const [chosen, setChosen] = useState<string | null>(null);
  const [stepDown, setStepDown] = useState(true);
  const [busy, setBusy] = useState(false);
  const others = (members.data ?? []).filter((m) => m.user_id !== me && m.role !== "owner");
  const picked = others.find((m) => m.user_id === chosen) ?? null;

  const transfer = async () => {
    if (picked === null || busy) return;
    setBusy(true);
    try {
      await api.PATCH("/api/v1/orgs/{org}/members/{user_id}", {
        params: { path: { org, user_id: picked.user_id } },
        body: { role: "owner" },
      });
      if (stepDown) {
        await api.PATCH("/api/v1/orgs/{org}/members/{user_id}", {
          params: { path: { org, user_id: me } },
          body: { role: "admin" },
        });
      }
      toast.success(
        stepDown
          ? `@${picked.login} owns it now; you're an admin`
          : `@${picked.login} is an owner too`,
      );
      setChosen(null);
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : "Ownership couldn't be handed on.");
    } finally {
      setBusy(false);
      await queryClient.invalidateQueries();
    }
  };

  return (
    <section className="flex flex-col gap-3" aria-labelledby={`${id}-title`}>
      <h2 id={`${id}-title`} className="font-medium">
        Hand on ownership
      </h2>
      <p className="text-sm text-fg-muted">
        Make another member an owner: they can then change anything here, this page included.
      </p>
      {members.isError ? (
        <ErrorState title="The members didn't load" onRetry={() => void members.refetch()} />
      ) : others.length === 0 ? (
        <p className="text-sm text-fg-faint">
          {members.isPending ? "Loading members…" : "There's no one else to hand it to yet."}
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          <Select
            value={chosen ?? ""}
            onValueChange={(value) => {
              setChosen(value === "" ? null : value);
            }}
          >
            <SelectTrigger aria-label="The new owner" className="w-64">
              <SelectValue>{picked ? `@${picked.login}` : "Choose a member"}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              {others.map((m) => (
                <SelectItem key={m.user_id} value={m.user_id}>
                  @{m.login} ({m.role})
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <div className="flex items-center gap-2">
            <Checkbox
              id={`${id}-step-down`}
              checked={stepDown}
              onCheckedChange={(value) => {
                setStepDown(value);
              }}
            />
            <Label htmlFor={`${id}-step-down`}>Then make me an admin</Label>
          </div>
          <div>
            <Button
              type="button"
              disabled={picked === null || busy}
              onClick={() => void transfer()}
            >
              Make owner
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}

function DeleteOrg({ org, name }: { org: string; name: string }) {
  const id = useId();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const remove = $api.useMutation("delete", "/api/v1/orgs/{org}");
  const confirmed = typed.trim().toLowerCase() === org;

  const go = async () => {
    if (!confirmed || remove.isPending) return;
    try {
      await remove.mutateAsync({ params: { path: { org } } });
      toast.success(`${name} deleted: restorable from your profile for ${GRACE_DAYS} days`);
      await queryClient.invalidateQueries();
      router.push("/settings/profile");
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : "It couldn't be deleted.");
    }
  };

  return (
    <section
      className="flex flex-col gap-3 rounded-lg border border-danger/40 p-4"
      aria-labelledby={`${id}-title`}
    >
      <h2 id={`${id}-title`} className="font-medium">
        Delete this organization
      </h2>
      <p className="text-sm text-fg-muted">
        It is hidden from everyone at once. Its owners may restore it for {GRACE_DAYS} days; then it
        is purged with everything in it: repositories, indexes, conversations, runs, keys.
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
          Delete {name}
        </Button>
      </div>
      <AlertDialog open={open} onOpenChange={setOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {name}?</AlertDialogTitle>
            <AlertDialogDescription>
              Type <span className="font-mono">{org}</span> to go on.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <Label htmlFor={`${id}-slug`} className="sr-only">
            The organization&apos;s name in URLs
          </Label>
          <Input
            id={`${id}-slug`}
            value={typed}
            placeholder={org}
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
              onClick={() => void go()}
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
