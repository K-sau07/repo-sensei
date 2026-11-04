/**
 * Each editor's MCP setup for this site (10.3, 10.5): the same configs as the MCP settings page
 * and `sensei mcp config` (lib/mcp-setup.ts), for the site's own origin.
 */
import { siteOrigin } from "@/lib/api/explore-server";
import { CLIENT_LABELS, MCP_CLIENTS, mcpSetup, type McpMode } from "@/lib/mcp-setup";

export function McpSetups({ mode = "remote" }: { mode?: McpMode }) {
  const server = siteOrigin();
  return (
    // Three across where there's room for them (the product page), else one under another.
    <div className="not-prose @container">
      <ul className="grid gap-4 @4xl:grid-cols-3">
        {MCP_CLIENTS.map((client) => {
          const setup = mcpSetup(client, server, mode);
          return (
            <li
              key={client}
              className="flex min-w-0 flex-col gap-2 rounded-xl border border-line bg-surface-1 p-4"
            >
              <p className="font-medium">{CLIENT_LABELS[client]}</p>
              <p className="font-mono text-xs text-fg-muted">{setup.file}</p>
              {/* Focusable: a block that scrolls sideways must be reachable by keyboard. */}
              <pre
                // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- as above
                tabIndex={0}
                aria-label={`${CLIENT_LABELS[client]}: ${setup.file}`}
                className="overflow-x-auto rounded-md bg-surface-2 p-3 font-mono text-xs"
              >
                {JSON.stringify(setup.config, null, 2)}
              </pre>
              {setup.command && (
                <pre
                  // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- as above
                  tabIndex={0}
                  aria-label={`${CLIENT_LABELS[client]}: the command`}
                  className="overflow-x-auto rounded-md bg-surface-2 p-3 font-mono text-xs whitespace-pre-wrap"
                >
                  {setup.command}
                </pre>
              )}
              <p className="text-xs text-fg-muted">{setup.then}</p>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
