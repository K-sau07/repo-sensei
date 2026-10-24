import { describe, expect, it } from "vitest";

import { codePath, orgPath, repoPath } from "@/lib/routes";

import { describeLocation, pathId } from "./locations";

describe("describeLocation", () => {
  it("names org pages", () => {
    expect(describeLocation("/acme")).toMatchObject({ kind: "page", title: "Home", org: "acme" });
    expect(describeLocation("/acme/inbox")).toMatchObject({
      id: "page:/acme/inbox",
      title: "Inbox",
      href: orgPath("acme", "inbox"),
    });
    expect(describeLocation("/acme/settings/members")).toMatchObject({ title: "Members" });
  });

  it("names a repository, its tabs, and its files with the ids search gives them", () => {
    expect(describeLocation("/acme/api")).toMatchObject({
      id: "repo:acme/api",
      kind: "repo",
      href: repoPath("acme", "api"),
    });
    expect(describeLocation("/acme/api/code")).toMatchObject({
      kind: "page",
      title: "Code",
      subtitle: "api",
      repo: "api",
    });
    const file = describeLocation("/acme/api/code/backend/app/main.py");
    expect(file).toEqual({
      id: pathId("acme", "api", "backend/app/main.py"),
      kind: "file",
      title: "main.py",
      subtitle: "api · backend/app",
      href: codePath("acme", "api", "backend/app/main.py"),
      org: "acme",
      repo: "api",
      path: "backend/app/main.py",
    });
    expect(describeLocation("/acme/api/code/backend/app")?.kind).toBe("dir");
    expect(describeLocation("/acme/api/code/backend/Dockerfile")?.kind).toBe("file");
  });

  it("decodes segments and gives the same href codePath builds", () => {
    const href = codePath("acme", "api", "notes/a file#1.md");
    expect(describeLocation(href)).toMatchObject({ path: "notes/a file#1.md", href });
  });

  it("skips pages the palette doesn't offer", () => {
    expect(describeLocation("/")).toBeNull();
    expect(describeLocation("/settings/profile")).toBeNull();
    expect(describeLocation("/acme/settings")).toBeNull(); // only redirects
    expect(describeLocation("/acme/api/nope")).toBeNull();
    expect(describeLocation("/acme/%zz")).toBeNull();
  });
});
