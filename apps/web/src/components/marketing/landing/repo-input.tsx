"use client";

/**
 * The landing page's one input (10.3): a public GitHub repository's URL. One already published
 * opens its public page; any other goes to be indexed in the visitor's organization (`/new`,
 * through sign-in when signed out).
 */
import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useId, useState, type SubmitEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { parseGitHubUrl } from "@/lib/github-url";
import { explorePath } from "@/lib/routes";

export function RepoInput({ published }: { published: readonly string[] }) {
  const router = useRouter();
  const id = useId();
  const [url, setUrl] = useState("");
  const [problem, setProblem] = useState(false);

  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    const repo = parseGitHubUrl(url);
    if (!repo) {
      setProblem(true);
      return;
    }
    const full = `${repo.owner}/${repo.name}`.toLowerCase();
    if (published.some((name) => name.toLowerCase() === full)) {
      router.push(explorePath(repo.owner, repo.name));
      return;
    }
    const canonical = `https://github.com/${repo.owner}/${repo.name}`;
    router.push(`/new?url=${encodeURIComponent(canonical)}` as Route);
  };

  return (
    <form onSubmit={submit} className="flex w-full max-w-xl flex-col gap-2" noValidate>
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
        <Button type="submit" size="lg" className="h-11">
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
