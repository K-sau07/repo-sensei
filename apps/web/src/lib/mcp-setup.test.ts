import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { MCP_CLIENTS, mcpSetup, shellQuote } from "./mcp-setup";

// The CLI's `sensei mcp config` output (its snapshots): the page shows the same setups.
const SNAPSHOTS = join(import.meta.dirname, "../../../../packages/cli/tests/snapshots");
const SERVER = "https://sensei.test";

/** The command after "Run:", and the JSON block, from a CLI snapshot. */
function fromCli(client: string, mode: string): { command: string | null; json: unknown } {
  const text = readFileSync(join(SNAPSHOTS, `mcp_${client}_${mode}.txt`), "utf8");
  const lines = text.split("\n");
  const run = lines.indexOf("Run:");
  const start = lines.indexOf("{");
  const end = lines.indexOf("}", start);
  return {
    command: run >= 0 ? (lines[run + 1] ?? "").trim() : null,
    json: JSON.parse(lines.slice(start, end + 1).join("\n")),
  };
}

describe("mcpSetup", () => {
  for (const client of MCP_CLIENTS) {
    for (const mode of ["remote", "local"] as const) {
      it(`matches the CLI for ${client} (${mode})`, () => {
        const setup = mcpSetup(client, SERVER, mode);
        const cli = fromCli(client, mode);
        expect(setup.config).toEqual(cli.json);
        expect(setup.command).toBe(cli.command);
      });
    }
  }

  it("uses each client's root key and keeps the key out of the file", () => {
    expect(Object.keys(mcpSetup("vscode", SERVER, "remote").config)).toEqual(["servers"]);
    expect(Object.keys(mcpSetup("cursor", SERVER, "remote").config)).toEqual(["mcpServers"]);
    expect(Object.keys(mcpSetup("claude-code", SERVER, "remote").config)).toEqual(["mcpServers"]);
    for (const client of MCP_CLIENTS) {
      expect(JSON.stringify(mcpSetup(client, SERVER, "local").config)).not.toMatch(
        /rs_[A-Za-z0-9]/,
      );
    }
  });

  it("quotes shell words as the CLI does", () => {
    expect(shellQuote("http://x.test/mcp")).toBe("http://x.test/mcp");
    expect(shellQuote(`{"a": 1}`)).toBe(`'{"a": 1}'`);
    expect(shellQuote("it's")).toBe(`'it'"'"'s'`);
  });
});
