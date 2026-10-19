"use client";

import { TriangleAlert } from "lucide-react";
import { useSearchParams } from "next/navigation";

import { buttonVariants } from "@/components/ui/button";
import { safeNext } from "@/lib/security/auth-paths";

import { GitHubMark } from "./github-mark";

// Error codes the API puts in /login?error=… after a failed sign-in.
const ERRORS: Record<string, string> = {
  access_denied: "You cancelled the sign-in on GitHub. Try again when you're ready.",
  state_mismatch:
    "This sign-in was started in another browser or tab. Start again here to continue.",
  invalid_state: "That sign-in link expired. Please start again.",
  github_not_configured: "GitHub sign-in isn't set up on this server yet.",
};
const GENERIC_ERROR = "Sign-in didn't complete. Please try again.";

export function loginUrl(next: string | null): string {
  const target = safeNext(next);
  return `/api/v1/auth/github/login${target ? `?next=${encodeURIComponent(target)}` : ""}`;
}

export function LoginPanel() {
  const params = useSearchParams();
  const errorCode = params.get("error");
  const message = errorCode ? (ERRORS[errorCode] ?? GENERIC_ERROR) : null;

  return (
    <div className="flex w-full max-w-sm flex-col gap-6">
      <header className="flex flex-col gap-2 text-center">
        <p className="font-mono text-xs tracking-widest text-signal uppercase">repo-sensei</p>
        <h1 className="text-2xl font-semibold tracking-[-0.015em]">Sign in</h1>
        <p className="text-sm text-fg-muted">
          Understand any codebase, with answers you can verify.
        </p>
      </header>

      {message && (
        <div
          role="alert"
          className="flex gap-2 rounded-lg border border-danger/30 bg-danger/8 p-3 text-sm"
        >
          <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-danger" />
          <p>{message}</p>
        </div>
      )}

      {/* A plain link: the API starts the OAuth flow and sets the state cookie. */}
      <a
        href={loginUrl(params.get("next"))}
        className={buttonVariants({ size: "lg", className: "w-full gap-2" })}
      >
        <GitHubMark className="size-4" />
        Continue with GitHub
      </a>

      <ul className="flex flex-col gap-2 text-xs text-fg-muted">
        <li>
          We read only your GitHub profile and email to sign you in. Code access is granted per
          repository when you install the GitHub App, and you can revoke it at any time.
        </li>
        <li>Nothing in your repositories changes without your explicit approval.</li>
      </ul>
    </div>
  );
}
