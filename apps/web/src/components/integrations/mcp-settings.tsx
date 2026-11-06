"use client";

import { useQuery } from "@tanstack/react-query";
import { CircleCheck, CircleX, PlugZap } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { CopyButton } from "@/components/primitives/copy-button";
import { useOrgSlug } from "@/components/shell/use-org-slug";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, fetchAfterMocks } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { McpCheck, McpTest } from "@/lib/api/types";
import {
  CLIENT_LABELS,
  MCP_CLIENTS,
  mcpSetup,
  type McpClient,
  type McpMode,
} from "@/lib/mcp-setup";
import { orgPath } from "@/lib/routes";

const METADATA = "/.well-known/oauth-protected-resource/mcp";

/** The MCP server's address, as editors find it: the protected resource metadata's `resource`
 * (on this origin, forwarded to the API). */
async function fetchResource(): Promise<string> {
  const response = await fetchAfterMocks(
    new Request(METADATA, { headers: { Accept: "application/json" } }),
  );
  if (!response.ok) throw new Error(`The sign-in document answered ${String(response.status)}.`);
  const found = (await response.json()) as { resource?: unknown };
  if (typeof found.resource !== "string") throw new Error("The sign-in document names no server.");
  return found.resource;
}

/** The server's origin from its MCP address (`…/mcp`). */
export function serverOf(resource: string): string {
  return resource.replace(/\/mcp\/?$/, "");
}

export type CheckName = McpCheck["name"] | "browser";

export const CHECK_LABELS: Record<CheckName, string> = {
  browser: "From your browser",
  challenge: "Sign-in challenge",
  metadata: "Sign-in document",
  tools: "Tools",
};

interface ShownCheck {
  name: CheckName;
  ok: boolean;
  detail: string;
}

function Snippet({ label, value, copyLabel }: { label: string; value: string; copyLabel: string }) {
  return (
    <div className="grid min-w-0 grid-cols-1 gap-1.5">
      <p className="text-sm">{label}</p>
      <div className="flex items-start gap-2 rounded-sm border border-line bg-surface-1 p-2">
        <pre className="min-w-0 flex-1 overflow-x-auto font-mono text-xs leading-relaxed">
          {value}
        </pre>
        <CopyButton value={value} label={copyLabel} />
      </div>
    </div>
  );
}

function ClientSetup({ client, server, org }: { client: McpClient; server: string; org: string }) {
  const [mode, setMode] = useState<McpMode>("remote");
  const setup = mcpSetup(client, server, mode);
  const name = CLIENT_LABELS[client];
  return (
    <div className="flex flex-col gap-5">
      <Tabs
        value={mode}
        onValueChange={(value: unknown) => {
          if (value === "remote" || value === "local") setMode(value);
        }}
      >
        <TabsList variant="line" aria-label={`How ${name} connects`}>
          <TabsTrigger value="remote">Sign in (recommended)</TabsTrigger>
          <TabsTrigger value="local">API key, run locally</TabsTrigger>
        </TabsList>
      </Tabs>
      <p className="max-w-prose text-sm text-fg-muted">
        {mode === "remote" ? (
          <>
            {name} connects to this server and asks you to sign in the first time, in your browser.
            You choose the organization and what it may do.
          </>
        ) : (
          <>
            {name} runs <code className="font-mono text-xs">sensei-mcp</code> on your computer with
            an{" "}
            <Link href={orgPath(org, "settings/api-keys")} className="underline underline-offset-2">
              API key
            </Link>
            . The key comes from your environment, never from this file, so it&apos;s safe to
            commit.
          </>
        )}
      </p>
      {setup.command && (
        <Snippet label="Run:" value={setup.command} copyLabel={`Copy the ${name} command`} />
      )}
      <Snippet
        label={setup.command ? `Or put this in ${setup.file}:` : `Put this in ${setup.file}:`}
        value={JSON.stringify(setup.config, null, 2)}
        copyLabel={`Copy ${setup.file}`}
      />
      <p className="text-sm">{setup.then}</p>
    </div>
  );
}

function ConnectionTest({ org, resource }: { org: string; resource: string | null }) {
  const [running, setRunning] = useState(false);
  const [checks, setChecks] = useState<ShownCheck[] | null>(null);
  const [tools, setTools] = useState<string[]>([]);

  const run = async () => {
    setRunning(true);
    const shown: ShownCheck[] = [];
    try {
      const reached = await fetchResource();
      shown.push({ name: "browser", ok: true, detail: `Reached ${reached}.` });
    } catch (error) {
      const said = error instanceof Error ? error.message : "Unreachable.";
      shown.push({ name: "browser", ok: false, detail: said });
    }
    try {
      const { data } = await api.POST("/api/v1/orgs/{org}/mcp/test", {
        params: { path: { org } },
      });
      const found: McpTest | undefined = data;
      shown.push(...(found?.checks ?? []));
      setTools(found?.tools ?? []);
    } catch (error) {
      const said = error instanceof ApiError ? error.message : "The check couldn't run.";
      shown.push({ name: "tools", ok: false, detail: said });
    }
    setChecks(shown);
    setRunning(false);
  };

  const ok = checks?.every((check) => check.ok);
  return (
    <section aria-labelledby="test-heading" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 id="test-heading" className="text-sm font-medium">
            Server
          </h3>
          <code className="font-mono text-xs text-fg-muted" data-testid="mcp-url">
            {resource ?? "…"}
          </code>
        </div>
        <Button variant="outline" disabled={running} onClick={() => void run()}>
          <PlugZap data-icon="inline-start" aria-hidden />
          {running ? "Testing…" : "Test connection"}
        </Button>
      </div>
      {checks && (
        <div role="status" className="rounded-lg border border-line">
          <p className="border-b border-line px-4 py-2 text-sm font-medium">
            {ok ? "Ready for your editor." : "Something's wrong: editors can't connect yet."}
          </p>
          <ul className="divide-y divide-line">
            {checks.map((check) => (
              <li key={check.name} className="flex items-start gap-3 px-4 py-2 text-sm">
                {check.ok ? (
                  <CircleCheck className="mt-0.5 size-4 shrink-0 text-ok" aria-label="Passed" />
                ) : (
                  <CircleX className="mt-0.5 size-4 shrink-0 text-danger" aria-label="Failed" />
                )}
                <span className="w-36 shrink-0 font-medium">{CHECK_LABELS[check.name]}</span>
                <span className="text-fg-muted">{check.detail}</span>
              </li>
            ))}
          </ul>
          {tools.length > 0 && (
            <p className="border-t border-line px-4 py-2 font-mono text-xs text-fg-muted">
              {tools.join(" · ")}
            </p>
          )}
        </div>
      )}
    </section>
  );
}

export function McpSettings() {
  const org = useOrgSlug();
  const resource = useQuery({ queryKey: ["mcp-resource"], queryFn: fetchResource, retry: 1 });
  // If it can't be had: this origin, which forwards /mcp (the test below says what's wrong).
  const server =
    resource.data !== undefined
      ? serverOf(resource.data)
      : resource.isError
        ? window.location.origin
        : null;

  return (
    <div className="flex flex-col gap-8">
      <header className="max-w-prose">
        <h2 className="text-lg font-semibold">Connect your editor</h2>
        <p className="text-sm text-fg-muted">
          Give your coding agent repo-sensei&apos;s tools over MCP: search, find symbols, read code,
          trace a feature across the layers, see what a change would break, and ask with citations.
          They answer from this organization&apos;s indexed repositories, with your access.
        </p>
      </header>
      <Tabs defaultValue="claude-code">
        <TabsList>
          {MCP_CLIENTS.map((client) => (
            <TabsTrigger key={client} value={client}>
              {CLIENT_LABELS[client]}
            </TabsTrigger>
          ))}
        </TabsList>
        {MCP_CLIENTS.map((client) => (
          <TabsContent key={client} value={client} className="pt-4">
            {server === null ? (
              <Skeleton aria-label="Loading the setup" className="h-48" />
            ) : (
              <ClientSetup client={client} server={server} org={org} />
            )}
          </TabsContent>
        ))}
      </Tabs>
      <ConnectionTest org={org} resource={resource.data ?? null} />
      <section aria-labelledby="cli-heading" className="flex flex-col gap-2">
        <h3 id="cli-heading" className="text-sm font-medium">
          From the command line
        </h3>
        <p className="text-sm text-fg-muted">
          The <code className="font-mono text-xs">sensei</code> command signs in the same way and
          prints these setups too.
        </p>
        {server !== null && (
          <Snippet
            label="Sign in, then print a setup:"
            value={`sensei login --server ${server}\nsensei mcp config --client claude-code`}
            copyLabel="Copy the sensei commands"
          />
        )}
      </section>
    </div>
  );
}
