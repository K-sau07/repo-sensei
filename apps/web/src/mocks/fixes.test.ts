import { describe, expect, it } from "vitest";

import { mockParse } from "@/mocks/handlers/fixes";

describe("the mock trace parser", () => {
  it("reads a Python traceback, mapping frames to the snapshot's files", () => {
    const ticket = mockParse(
      [
        "Traceback (most recent call last):",
        '  File "/app/backend/app/api/routes/items.py", line 34, in read_items',
        '  File "/usr/lib/python3.13/site-packages/sqlalchemy/orm/session.py", line 9, in run',
        "ZeroDivisionError: integer division or modulo by zero",
      ].join("\n"),
    );
    expect(ticket.error_type).toBe("ZeroDivisionError");
    expect(ticket.title).toBe("ZeroDivisionError: integer division or modulo by zero");
    expect(ticket.frames.map((f) => [f.path, f.in_repo, f.library])).toEqual([
      ["backend/app/api/routes/items.py", true, false],
      [null, false, true],
    ]);
    expect(ticket.culprit?.path).toBe("backend/app/api/routes/items.py");
  });

  it("reads a JavaScript stack outermost first, and keeps a given title", () => {
    const ticket = mockParse(
      [
        "TypeError: Cannot read properties of undefined (reading 'map')",
        "    at ItemsTable (http://localhost:5173/src/routes/_layout/items.tsx:58:21)",
        "    at renderWithHooks (node_modules/react-dom/cjs/react-dom.development.js:1:1)",
      ].join("\n"),
      "Items table crashes",
    );
    expect(ticket.title).toBe("Items table crashes");
    expect(ticket.language).toBe("javascript");
    expect(ticket.frames.map((f) => f.function)).toEqual(["renderWithHooks", "ItemsTable"]);
    expect(ticket.culprit?.path).toBe("frontend/src/routes/_layout/items.tsx");
  });
});
