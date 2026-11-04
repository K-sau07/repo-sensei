"use client";

/**
 * What came of installing the GitHub App, said where GitHub sends the person back (onboarding,
 * Connect a repository, the integrations settings): the API's `?installed=1`, or its
 * `?github_error=` code in words.
 */
import { useQueryState } from "nuqs";

export const GITHUB_ERRORS: Record<string, string> = {
  installation_not_owned:
    "That installation belongs to an account you don't administer. Install it on your own account or an organization you're an admin of.",
  installation_unsupported: "Enterprise installations aren't supported yet.",
  github_unavailable: "GitHub didn't answer. Try again in a moment.",
  install_requested:
    "The installation is waiting for an owner of that GitHub account to approve it. It appears here once they do.",
  install_missing: "GitHub didn't say what was installed. Try installing again.",
};

export function githubErrorMessage(code: string): string {
  return GITHUB_ERRORS[code] ?? "No access yet: the installation didn't finish.";
}

export function InstallResult() {
  const [error] = useQueryState("github_error");
  const [installed] = useQueryState("installed");
  if (error) {
    return (
      <p role="alert" className="text-sm text-danger" data-testid="install-result">
        {githubErrorMessage(error)}
      </p>
    );
  }
  if (installed === "1") {
    return (
      <p role="status" className="text-sm text-ok" data-testid="install-result">
        The GitHub App is installed.
      </p>
    );
  }
  return null;
}
