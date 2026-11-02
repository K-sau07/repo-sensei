/**
 * The mock MCP server's discovery and the settings page's connection check (7.5). Set
 * `localStorage["sensei.mock.mcp"] = "broken"` to see a failing check.
 */
import { http, HttpResponse } from "msw";

import type { McpTest } from "@/lib/api/types";

import { orgAccess } from "./orgs";

const TOOLS = [
  "sensei_ask",
  "sensei_find_symbol",
  "sensei_graph_neighbors",
  "sensei_impact",
  "sensei_list_repos",
  "sensei_read_code",
  "sensei_search",
  "sensei_trace",
];

const broken = () =>
  typeof localStorage !== "undefined" && localStorage.getItem("sensei.mock.mcp") === "broken";

export const mcpHandlers = [
  http.get("*/.well-known/oauth-protected-resource/mcp", () =>
    HttpResponse.json({
      resource: `${location.origin}/mcp`,
      authorization_servers: [location.origin],
      scopes_supported: ["read:repo", "ask", "connect:repo"],
      bearer_methods_supported: ["header"],
      resource_name: "repo-sensei",
    }),
  ),

  http.post<{ org: string }>("*/api/v1/orgs/:org/mcp/test", ({ params }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    const fails = broken();
    return HttpResponse.json<McpTest>({
      ok: !fails,
      url: `${location.origin}/mcp`,
      server_name: "repo-sensei",
      protocol_version: "2025-11-25",
      tools: fails ? [] : TOOLS,
      checks: [
        { name: "challenge", ok: true, detail: "Asks editors to sign in, and says where." },
        { name: "metadata", ok: true, detail: `Sign-in is at ${location.origin}.` },
        fails
          ? { name: "tools", ok: false, detail: "The session failed: TimeoutError." }
          : { name: "tools", ok: true, detail: `${String(TOOLS.length)} tools.` },
      ],
      ms: 42,
    });
  }),
];
