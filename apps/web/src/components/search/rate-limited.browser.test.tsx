import { describe, expect, it } from "vitest";
import { render } from "vitest-browser-react";

import { ApiError } from "@/lib/api/errors";

import { RateLimited } from "./rate-limited";

const tooMany = (retryAfter: number) =>
  new ApiError({ status: 429, code: "rate_limited", message: "Too many searches.", retryAfter });

describe("a rate limit", () => {
  it("counts down again when a retry gets another 429", async () => {
    const retry = () => undefined;
    const screen = await render(<RateLimited error={tooMany(1)} onRetry={retry} />);
    const button = screen.getByRole("button", { name: "Try again" });
    await expect.element(button).toBeDisabled();
    await expect
      .element(screen.getByText("You can search again now."), { timeout: 3000 })
      .toBeVisible();
    await expect.element(button).toBeEnabled();

    await screen.rerender(<RateLimited error={tooMany(2)} onRetry={retry} />);
    await expect
      .element(screen.getByText(/^You can search again in [12] seconds?\.$/))
      .toBeVisible();
    await expect.element(button).toBeDisabled();
  });
});
