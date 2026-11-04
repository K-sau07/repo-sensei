/**
 * A GitHub repository URL as a person types it: the same rules as the API's
 * (packages/core/src/sensei_core/repos/urls.py, parse_github_url), checked here first so the
 * landing page can say at once what it can't read. The API stays the authority.
 */
const OWNER = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/;
const NAME = /^[A-Za-z0-9_.-]{1,100}$/;
const SSH = /^git@(?<host>[^:/\s]+):(?<path>\S+)$/;
const HOSTS = new Set(["github.com", "www.github.com"]);

export interface GitHubRepoName {
  owner: string;
  name: string;
}

/** `{owner, name}` from `https://github.com/owner/name` and its usual variants (no scheme,
 * `.git`, a trailing `/tree/main`, `git@github.com:o/n.git`); null for anything else. */
export function parseGitHubUrl(value: string): GitHubRepoName | null {
  const text = value.trim();
  let host: string;
  let path: string;
  const ssh = SSH.exec(text);
  if (ssh?.groups) {
    host = (ssh.groups.host ?? "").toLowerCase();
    path = ssh.groups.path ?? "";
  } else {
    const full = text.includes("://") ? text : `https://${text}`;
    let url: URL;
    try {
      url = new URL(full);
    } catch {
      return null;
    }
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    // As written, not as the browser normalises it: a port (":443" too, which URL drops) is
    // refused, and "a/../b" is read as its segments (URL would make it "b"), as the API reads.
    const rest = full.slice(full.indexOf("://") + 3);
    const authority = rest.split(/[/?#]/, 1)[0] ?? "";
    if (url.username || url.password || /:\d+$/.test(authority)) return null;
    host = url.hostname.toLowerCase();
    path = rest.slice(authority.length).split(/[?#]/, 1)[0] ?? "";
  }
  if (!HOSTS.has(host)) return null;
  const [owner, rawName] = path.split("/").filter(Boolean);
  if (owner === undefined || rawName === undefined) return null;
  const name = rawName.endsWith(".git") ? rawName.slice(0, -".git".length) : rawName;
  if (!OWNER.test(owner) || !NAME.test(name) || name === "." || name === "..") return null;
  return { owner, name };
}
