import { describe, expect, it } from "vitest";
import { render } from "vitest-browser-react";

import { Marked, SnippetView } from "./snippet";

describe("snippet rendering", () => {
  it("renders code as text: markup in a line is never parsed", async () => {
    const line = '<img src="x" onerror="alert(1)"> <b>bold</b>';
    const screen = await render(
      <p data-testid="line">
        <Marked text={line} ranges={[{ start: 1, end: 4 }]} />
      </p>,
    );
    const element = screen.getByTestId("line").element();
    expect(element.querySelector("img")).toBeNull();
    expect(element.querySelector("b")).toBeNull();
    expect(element.textContent).toBe(line);
    expect(element.querySelector("mark")?.textContent).toBe("img");
  });

  it("marks each highlight on its line, clipped and merged, with an emoji before one", async () => {
    const screen = await render(
      <SnippetView
        maxLines={12}
        snippet={{
          start_line: 10,
          lines: ["🚀 deploy the user", "plain", "user_email user"],
          highlights: [
            { line: 10, start: 14, end: 18 },
            { line: 12, start: 0, end: 4 },
            { line: 12, start: 2, end: 10 },
            { line: 12, start: 11, end: 99 },
            { line: 40, start: 0, end: 3 },
          ],
        }}
      />,
    );
    const marks = [...screen.container.querySelectorAll("mark")].map((mark) => mark.textContent);
    expect(marks).toEqual(["user", "user_email", "user"]);
  });

  it("shows a window of a long line around its first match", async () => {
    const long = `${"a".repeat(900)}needle${"b".repeat(900)}`;
    const screen = await render(
      <SnippetView
        maxLines={4}
        snippet={{ start_line: 1, lines: [long], highlights: [{ line: 1, start: 900, end: 906 }] }}
      />,
    );
    const code = screen.container.querySelector("code");
    expect(code?.querySelector("mark")?.textContent).toBe("needle");
    expect(code?.textContent.startsWith("…")).toBe(true);
    expect(code?.textContent.endsWith("…")).toBe(true);
    expect((code?.textContent ?? "").length).toBeLessThan(200);
  });
});
