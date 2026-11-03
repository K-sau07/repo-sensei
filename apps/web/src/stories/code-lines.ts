import { expect, waitFor } from "storybook/test";

/** What the viewer under `root` shows, for a failure's message: whether there's an editor, the
 * lines it drew (the first few), and whether the page is visible (a hidden one may not draw). */
function described(root: HTMLElement): string {
  const lines = [...root.querySelectorAll<HTMLElement>(".cm-line")].map((el) => el.textContent);
  const editor = root.querySelector(".cm-editor") !== null;
  const loading = root.querySelector('[aria-label="Loading the code"]') !== null;
  const first = lines.slice(0, 3).map((text) => JSON.stringify(text.slice(0, 60)));
  return [
    `editor: ${editor ? "yes" : "no"}${loading ? " (still loading)" : ""}`,
    `${String(lines.length)} lines drawn${first.length ? `, from ${first.join(", ")}` : ""}`,
    `page ${document.visibilityState}`,
  ].join("; ");
}

/**
 * Waits for the code viewer to show a line matching `pattern`, visible. A line is read whole
 * (its text across the spans syntax highlighting splits it into) and found again on every try:
 * the editor redraws its lines as it highlights and scrolls, replacing their elements.
 */
export async function expectCodeLine(root: HTMLElement, pattern: RegExp, timeout: number) {
  try {
    await waitFor(
      async () => {
        const line = [...root.querySelectorAll<HTMLElement>(".cm-line")].find((el) =>
          pattern.test(el.textContent),
        );
        await expect(line).toBeVisible();
      },
      { timeout },
    );
  } catch (error) {
    throw new Error(
      `No line matching ${String(pattern)} in ${String(timeout)} ms: ${described(root)}`,
      {
        cause: error,
      },
    );
  }
}
