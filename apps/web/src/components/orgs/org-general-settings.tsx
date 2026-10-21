"use client";

import { useQueryClient } from "@tanstack/react-query";
import { TriangleAlert } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState, type SubmitEvent } from "react";
import { toast } from "sonner";

import { ErrorState } from "@/components/feedback/error-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { $api, api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { Me, Org, Role } from "@/lib/api/types";
import { SLUG_MESSAGES } from "@/lib/orgs/slug";
import { queryKeys } from "@/lib/query/keys";
import { orgPath } from "@/lib/routes";

import { SlugField, useSlugStatus } from "./slug-field";

/** ``me`` with the org at ``oldSlug`` also listed under its saved slug (both work until the
 * next refetch). */
export function withRenamedOrg(me: Me, oldSlug: string, saved: Org): Me {
  const old = me.orgs.find((o) => o.slug === oldSlug);
  if (!old) return me;
  return {
    ...me,
    orgs: [...me.orgs, { ...old, slug: saved.slug, name: saved.name }],
    last_org_slug: saved.slug,
  };
}

export function OrgGeneralSettings() {
  const { slug, role } = useOrgRole();
  const org = $api.useQuery("get", "/api/v1/orgs/{org}", { params: { path: { org: slug } } });

  if (org.isPending) {
    return (
      <div role="status" aria-label="Loading settings" className="flex flex-col gap-3">
        <Skeleton className="h-control-md" />
        <Skeleton className="h-control-md" />
      </div>
    );
  }
  if (org.isError) {
    return <ErrorState title="Couldn't load settings" onRetry={() => void org.refetch()} />;
  }
  // Keyed on the saved values, so the form starts over after every save.
  return (
    <GeneralForm
      key={`${org.data.slug}:${org.data.name}`}
      org={org.data}
      role={role}
      onSaved={() => void org.refetch()}
    />
  );
}

function GeneralForm({ org, role, onSaved }: { org: Org; role: Role | null; onSaved: () => void }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [name, setName] = useState(org.name);
  const [newSlug, setNewSlug] = useState(org.slug);
  const [saving, setSaving] = useState(false);
  const canEdit = roleAtLeast(role, "admin");
  const slugStatus = useSlugStatus(newSlug, org.slug);

  const slugChanged = newSlug !== org.slug;
  const changed = name.trim() !== org.name || slugChanged;
  const slugOk = !slugChanged || slugStatus.status === "available";
  const canSave = canEdit && changed && name.trim() !== "" && slugOk && !saving;

  const save = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSave) return;
    setSaving(true);
    try {
      const { data } = await api.PATCH("/api/v1/orgs/{org}", {
        params: { path: { org: org.slug } },
        body: { name: name.trim(), ...(slugChanged ? { slug: newSlug } : {}) },
      });
      toast.success("Settings saved");
      if (data && data.slug !== org.slug) {
        // While the URL moves from the old slug to the new one, both must be valid, or the org
        // gate shows "not found" for a moment. Add the new slug to the cached /me, mark it stale
        // without refetching, and navigate: the next page refetches the truth once it's there.
        queryClient.setQueryData<Me>(
          queryKeys.me(),
          (me) => me && withRenamedOrg(me, org.slug, data),
        );
        void queryClient.invalidateQueries({ queryKey: queryKeys.me(), refetchType: "none" });
        router.replace(orgPath(data.slug, "settings/general"));
      } else {
        await queryClient.invalidateQueries({ queryKey: queryKeys.me() });
        onSaved();
      }
    } catch (error) {
      toast.error(
        error instanceof ApiError ? (SLUG_MESSAGES[error.code] ?? error.message) : "Couldn't save.",
      );
      setSaving(false);
    }
  };

  return (
    <form onSubmit={(event) => void save(event)} className="flex max-w-lg flex-col gap-6">
      <header>
        <h2 className="text-lg font-semibold">General</h2>
        {!canEdit && (
          <p className="text-sm text-fg-muted">
            Only admins and owners can change these. You&rsquo;re a {role ?? "member"} here.
          </p>
        )}
      </header>
      <div className="flex items-center gap-4">
        <span
          aria-hidden
          className="flex size-14 items-center justify-center rounded-lg bg-surface-3 text-xl font-semibold"
        >
          {org.name.charAt(0).toUpperCase()}
        </span>
        <p className="text-sm text-fg-muted">
          The organization picture comes from GitHub once the GitHub App is installed.
        </p>
      </div>
      <div className="grid gap-2">
        <Label htmlFor="settings-name">Name</Label>
        <Input
          id="settings-name"
          value={name}
          maxLength={100}
          disabled={!canEdit}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </div>
      <SlugField
        id="settings-slug"
        value={newSlug}
        current={org.slug}
        disabled={!canEdit}
        onChange={setNewSlug}
      />
      {slugChanged && (
        <p className="flex gap-2 rounded-lg border border-warn/30 bg-warn/8 p-3 text-sm">
          <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-warn" />
          Changing the URL breaks links and bookmarks to the old one. Shared links, MCP configs and
          CLI settings that use it need updating.
        </p>
      )}
      <div>
        <Button type="submit" disabled={!canSave}>
          {saving ? "Saving…" : "Save changes"}
        </Button>
      </div>
    </form>
  );
}
