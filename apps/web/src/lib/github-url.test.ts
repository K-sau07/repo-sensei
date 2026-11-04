import { describe, expect, it } from "vitest";

import { parseGitHubUrl } from "./github-url";

describe("parseGitHubUrl", () => {
  it.each([
    [
      "https://github.com/fastapi/full-stack-fastapi-template",
      "fastapi",
      "full-stack-fastapi-template",
    ],
    ["github.com/langflow-ai/langflow", "langflow-ai", "langflow"],
    ["https://www.github.com/getsentry/sentry.git", "getsentry", "sentry"],
    ["https://github.com/o/n/tree/main/src", "o", "n"],
    ["git@github.com:owner/name.git", "owner", "name"],
    ["https://github.com/o/n/../x", "o", "n"], // as the API reads it: its first two segments
    ["https://github.com/o/n?x=1#y", "o", "n"],
    ["  http://GitHub.com/a/b  ", "a", "b"],
  ])("reads %s", (value, owner, name) => {
    expect(parseGitHubUrl(value)).toEqual({ owner, name });
  });

  it.each([
    "",
    "fastapi",
    "https://gitlab.com/o/n",
    "https://github.com/only-owner",
    "https://user:pass@github.com/o/n",
    "https://github.com:8443/o/n",
    "ftp://github.com/o/n",
    "https://github.com/-bad/n",
    "https://github.com/o/..",
    "https://github.evil.example/o/n",
    "https://github.com:443/o/n",
    "https://github.com/a/../b/c",
    "github.com/o/./n",
  ])("refuses %s", (value) => {
    expect(parseGitHubUrl(value)).toBeNull();
  });
});
