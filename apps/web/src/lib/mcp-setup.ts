/**
 * How to connect an editor's agent to repo-sensei's MCP server (7.5): the same setups as the CLI's
 * `sensei mcp config` (packages/cli `editors.py`).
 *
 * - remote: the server's `/mcp` over HTTP; the editor signs in with OAuth by itself the first
 *   time (the server's 401 says where);
 * - local: `sensei-mcp` run by the editor over stdio, with an API key from the environment (or,
 *   in VS Code, asked for once and kept in its secret storage). The key is never written into
 *   the project's config file, which may be committed.
 *
 * Each client's file and root key: Claude Code `.mcp.json` (`mcpServers`), VS Code
 * `.vscode/mcp.json` (`servers`), Cursor `.cursor/mcp.json` (`mcpServers`).
 */

export const MCP_CLIENTS = ["claude-code", "vscode", "cursor"] as const;
export type McpClient = (typeof MCP_CLIENTS)[number];
export type McpMode = "remote" | "local";

export const CLIENT_LABELS: Record<McpClient, string> = {
  "claude-code": "Claude Code",
  vscode: "VS Code",
  cursor: "Cursor",
};

const NAME = "repo-sensei";
const COMMAND = "sensei-mcp";
const KEY_INPUT = "sensei-api-key";

export interface McpSetup {
  /** Where the JSON goes, in the project. */
  file: string;
  config: Record<string, unknown>;
  /** A one-liner that adds it instead. */
  command: string | null;
  /** What to do next. */
  then: string;
}

/** A shell word, quoted when it needs to be (as Python's `shlex.quote`). */
export function shellQuote(word: string): string {
  if (/^[\w@%+=:,./-]+$/.test(word)) return word;
  return `'${word.replaceAll("'", `'"'"'`)}'`;
}

/** `server`: the origin editors reach (its `/mcp` is the MCP server). */
export function mcpSetup(client: McpClient, server: string, mode: McpMode): McpSetup {
  const url = `${server}/mcp`;
  const local = mode === "local";
  switch (client) {
    case "claude-code": {
      if (!local) {
        return {
          file: ".mcp.json",
          config: { mcpServers: { [NAME]: { type: "http", url } } },
          command: `claude mcp add --transport http ${NAME} ${shellQuote(url)}`,
          then: `In Claude Code, run /mcp and choose ${NAME} to sign in.`,
        };
      }
      const env = { SENSEI_URL: server, SENSEI_API_KEY: "${SENSEI_API_KEY}" };
      return {
        file: ".mcp.json",
        config: { mcpServers: { [NAME]: { type: "stdio", command: COMMAND, env } } },
        command:
          `claude mcp add --env SENSEI_URL=${shellQuote(server)} ` +
          `--env SENSEI_API_KEY="$SENSEI_API_KEY" --transport stdio ${NAME} -- ${COMMAND}`,
        then: "Set SENSEI_API_KEY in the environment Claude Code starts from.",
      };
    }
    case "vscode": {
      if (!local) {
        const entry = { type: "http", url };
        return {
          file: ".vscode/mcp.json",
          config: { servers: { [NAME]: entry } },
          command: `code --add-mcp ${shellQuote(JSON.stringify({ name: NAME, ...entry }))}`,
          then: "Start the server from the MCP view and sign in when VS Code asks.",
        };
      }
      const prompt = {
        type: "promptString",
        id: KEY_INPUT,
        description: "repo-sensei API key",
        password: true,
      };
      const env = { SENSEI_URL: server, SENSEI_API_KEY: `\${input:${KEY_INPUT}}` };
      return {
        file: ".vscode/mcp.json",
        config: {
          inputs: [prompt],
          servers: { [NAME]: { type: "stdio", command: COMMAND, env } },
        },
        command: null,
        then: "VS Code asks for the API key the first time the server starts, and keeps it.",
      };
    }
    case "cursor": {
      if (!local) {
        return {
          file: ".cursor/mcp.json",
          config: { mcpServers: { [NAME]: { url } } },
          command: null,
          then: `In Cursor's MCP settings, sign in to ${NAME} when it asks.`,
        };
      }
      const env = { SENSEI_URL: server, SENSEI_API_KEY: "${env:SENSEI_API_KEY}" };
      return {
        file: ".cursor/mcp.json",
        config: { mcpServers: { [NAME]: { type: "stdio", command: COMMAND, env } } },
        command: null,
        then: "Set SENSEI_API_KEY in the environment Cursor starts from.",
      };
    }
  }
}
