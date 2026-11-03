"use client";

/**
 * On the profile page (F19): the deleted orgs the user owns that can still be restored, and
 * deleting the account itself: refused while they are an org's only owner (the orgs are named,
 * each linked to where ownership is handed on), confirmed by typing their GitHub login.
 */
import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useId, useState } from "react";
import { toast } from "sonner";

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
import { $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { orgPath } from "@/lib/routes";

const DAY = new Intl.DateTimeFormat("en", { dateStyle: "medium" });

export function DeletedOrgs() {
  const id = useId();
  const queryClient = useQueryClient();
  const deleted = $api.useQuery("get", "/api/v1/orgs/deleted");
  const restore = $api.useMutation("post", "/api/v1/orgs/{org}/restore");
  if (!deleted.data?.length) return null;

  const bring = async (slug: string, name: string) => {
    try {
      await restore.mutateAsync({ params: { path: { org: slug } } });
      toast.success(`${name} is back`);
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : "It couldn't be restored.");
    } finally {
      await queryClient.invalidateQueries();
    }
  };

  return (
    <section className="flex flex-col gap-3" aria-labelledby={`${id}-title`}>
      <h2 id={`${id}-title`} className="font-medium">
        Deleted organizations
      </h2>
      <ul className="flex flex-col divide-y divide-line rounded-lg border border-line">
        {deleted.data.map((org) => (
          <li key={org.slug} className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm">
            <span className="font-medium">{org.name}</span>
            <span className="text-fg-muted">purged on {DAY.format(new Date(org.purge_at))}</span>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="ml-auto"
              disabled={restore.isPending}
              onClick={() => void bring(org.slug, org.name)}
            >
              Restore {org.name}
            </Button>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function DeleteAccount({ login }: { login: string }) {
  const id = useId();
  const queryClient = useQueryClient();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [sole, setSole] = useState<string[]>([]);
  const remove = $api.useMutation("delete", "/api/v1/me");
  const confirmed = typed.trim().toLowerCase() === login.toLowerCase();

  const go = async () => {
    if (!confirmed || remove.isPending) return;
    try {
      await remove.mutateAsync({ body: { confirm: typed.trim() } });
      queryClient.clear(); // nothing of the account is kept on the page
      router.replace("/login");
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "sole_owner") {
        const details = caught.details as { orgs?: unknown } | null;
        setSole(Array.isArray(details?.orgs) ? details.orgs.map(String) : []);
        setOpen(false);
        return;
      }
      toast.error(caught instanceof ApiError ? caught.message : "The account couldn't be deleted.");
    }
  };

  return (
    <section
      className="flex flex-col gap-3 rounded-lg border border-danger/40 p-4"
      aria-labelledby={`${id}-title`}
    >
      <h2 id={`${id}-title`} className="font-medium">
        Delete your account
      </h2>
      <p className="text-sm text-fg-muted">
        You&apos;re signed out everywhere and leave every organization; your name, login, email and
        picture are removed. What you wrote in an organization (questions, diagrams, runs) stays
        with it, by no one.
      </p>
      {sole.length > 0 && (
        <div role="alert" className="text-sm text-danger">
          You&apos;re the only owner of{" "}
          {sole.map((slug, i) => (
            <span key={slug}>
              {i > 0 && ", "}
              <Link href={orgPath(slug, "settings/danger")} className="underline">
                {slug}
              </Link>
            </span>
          ))}
          : make someone else an owner, or delete it, first.
        </div>
      )}
      <div>
        <Button
          type="button"
          variant="destructive"
          onClick={() => {
            setTyped("");
            setOpen(true);
          }}
        >
          Delete my account
        </Button>
      </div>
      <AlertDialog open={open} onOpenChange={setOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete your account?</AlertDialogTitle>
            <AlertDialogDescription>
              This can&apos;t be undone. Type <span className="font-mono">{login}</span> to go on.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <Label htmlFor={`${id}-login`} className="sr-only">
            Your GitHub login
          </Label>
          <Input
            id={`${id}-login`}
            value={typed}
            placeholder={login}
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
              Delete my account
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
