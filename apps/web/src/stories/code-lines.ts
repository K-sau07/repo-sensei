import { expect, waitFor } from "storybook/test";

/**
 * Waits for the code viewer to show a line matching `pattern`, visible. A line is read whole
 * (its text across the spans syntax highlighting splits it into) and found again on every try:
 * the editor redraws its lines as it highlights and scrolls, replacing their elements.
 */
export async function expectCodeLine(root: HTMLElement, pattern: RegExp, timeout: number) {
  await waitFor(
    async () => {
      const line = [...root.querySelectorAll<HTMLElement>(".cm-line")].find((el) =>
        pattern.test(el.textContent),
      );
      await expect(line).toBeVisible();
    },
    { timeout },
  );
}
