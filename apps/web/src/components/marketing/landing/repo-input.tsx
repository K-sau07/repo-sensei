"use client";

/**
 * The landing page's one input (10.3): a public GitHub repository's URL. One already published
 * (the API says, when asked) opens its public page; any other goes to be indexed in the
 * visitor's organization (`/new`, through sign-in when signed out).
 */
import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useId, useState, type SubmitEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetchAfterMocks } from "@/lib/api/client";
import { parseGitHubUrl } from "@/lib/github-url";
import { explorePath } from "@/lib/routes";

/** Whether the repository is published here (anything but a yes, the API failing too, is a no:
 * it goes to be indexed instead). */
async function isPublished(owner: string, name: string): Promise<boolean> {
  const path = `/api/v1/public/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`;
  try {
    const response = await fetchAfterMocks(
      new Request(path, { headers: { accept: "application/json" } }),
    );
    return response.ok;
  } catch {
    return false;
  }
}

export function RepoInput() {
  const router = useRouter();
  const id = useId();
  const [url, setUrl] = useState("");
  const [problem, setProblem] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    const repo = parseGitHubUrl(url);
    if (!repo) {
      setProblem(true);
      return;
    }
    setBusy(true);
    if (await isPublished(repo.owner, repo.name)) {
      router.push(explorePath(repo.owner, repo.name));
      return;
    }
    const canonical = `https://github.com/${repo.owner}/${repo.name}`;
    router.push(`/new?url=${encodeURIComponent(canonical)}` as Route);
  };

  return (
    <form
      onSubmit={(event) => void submit(event)}
      className="flex w-full max-w-xl flex-col gap-2 text-left"
      noValidate
    >
      <div className="flex flex-col gap-2 sm:flex-row">
        <Input
          aria-label="A public GitHub repository's URL"
          aria-invalid={problem || undefined}
          aria-describedby={problem ? `${id}-problem` : undefined}
          placeholder="https://github.com/owner/repository"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          value={url}
          className="h-11 font-mono text-sm sm:flex-1"
          onChange={(event) => {
            setUrl(event.target.value);
            setProblem(false);
          }}
        />
        <Button type="submit" size="lg" className="h-11" disabled={busy}>
          Understand it
        </Button>
      </div>
      {problem && (
        <p id={`${id}-problem`} role="alert" className="text-sm text-danger">
          That isn&apos;t a GitHub repository&apos;s URL. Paste one like
          https://github.com/owner/name.
        </p>
      )}
    </form>
  );
}
