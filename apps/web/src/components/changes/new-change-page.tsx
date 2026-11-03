"use client";

/**
 * `/[org]/[repo]/changes/new` (9.3): describe a change. Suggestions start one; scope chips say
 * which layers it should stay in (said in the request, as words the planner reads). It is
 * planned file by file for a person to approve before anything is written; viewers can't ask.
 */
import { GitPullRequestArrow } from "lucide-react";
import { useRouter } from "next/navigation";
import { useId, useState, type SubmitEvent } from "react";

import { PermissionState } from "@/components/feedback/permission-state";
import { useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { LAYER_LABELS, SCOPE_LAYERS, type ScopeLayer } from "@/lib/api/ask";
import { requestText } from "@/lib/api/changes";
import { ApiError } from "@/lib/api/errors";
import { repoPath } from "@/lib/routes";

import { useCreateChange } from "./use-changes";

const MAX_REQUEST = 4_000;
export const SUGGESTIONS = [
  "Add rate limiting to the API: 60 requests a minute per client, 429 after that.",
  "Add pagination to the items list endpoint (limit and cursor), and use it in the page.",
  "Log every failed sign-in with the user and the reason.",
  "Add a created_at sort to the items table.",
] as const;

export function NewChangePage() {
  const { role, me } = useOrgRole();
  if (me.isPending) return null;
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold">New change</h1>
        <p className="text-sm text-fg-muted">
          Describe what should change. It is planned file by file, with the tests it adds, for you
          to edit and approve before anything is written; the result is a draft pull request, never
          a merge.
        </p>
      </div>
      {roleAtLeast(role, "member") ? (
        <Describe />
      ) : (
        <PermissionState requiredRole="member" {...(role ? { yourRole: role } : {})} />
      )}
    </div>
  );
}

function Describe() {
  const id = useId();
  const { org, repo } = useRepoSlug();
  const router = useRouter();
  const create = useCreateChange();
  const [text, setText] = useState("");
  const [layers, setLayers] = useState<ScopeLayer[]>([]);
  const [error, setError] = useState<string | null>(null);
  const submit = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    try {
      const made = await create.start({ request: requestText(text, layers) });
      router.push(repoPath(org, repo, `changes/${made.id}`));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The change couldn't be started.");
    }
  };
  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={id}>What should change?</Label>
        <Textarea
          id={id}
          value={text}
          maxLength={MAX_REQUEST - 120}
          rows={6}
          onChange={(event) => {
            setText(event.target.value);
          }}
          placeholder="What it should do, and why. Name a page, route or file if you know it."
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <span className="text-xs text-fg-muted" id={`${id}-suggestions`}>
          Or start from one of these
        </span>
        <ul className="flex flex-wrap gap-1.5" aria-labelledby={`${id}-suggestions`}>
          {SUGGESTIONS.map((suggestion) => (
            <li key={suggestion}>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-auto py-1 text-left text-xs whitespace-normal"
                onClick={() => {
                  setText(suggestion);
                }}
              >
                {suggestion}
              </Button>
            </li>
          ))}
        </ul>
      </div>
      <div className="flex flex-col gap-1.5">
        <span className="text-xs text-fg-muted" id={`${id}-scope`}>
          Keep it to (optional)
        </span>
        <ToggleGroup
          multiple
          value={layers}
          onValueChange={(value: string[]) => {
            setLayers(SCOPE_LAYERS.filter((layer) => value.includes(layer)));
          }}
          aria-labelledby={`${id}-scope`}
        >
          {SCOPE_LAYERS.map((layer) => (
            <ToggleGroupItem key={layer} value={layer} size="sm">
              {LAYER_LABELS[layer]}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      </div>
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
      <div>
        <Button type="submit" disabled={create.isPending || !text.trim()}>
          <GitPullRequestArrow aria-hidden />
          {create.isPending ? "Starting…" : "Plan this change"}
        </Button>
      </div>
    </form>
  );
}
