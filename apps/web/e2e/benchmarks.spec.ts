import { expect, test } from "./fixtures";

test.use({ signedIn: false });

test("the benchmarks page shows CrossLayerBench's results and where the flows are", async ({
  page,
}) => {
  await page.goto("/benchmarks");
  await expect(page.getByRole("heading", { name: "Benchmarks", level: 1 })).toBeVisible();
  const table = page.getByRole("table", { name: "CrossLayerBench v1 results by repository" });
  // Every repository, and the renamed copies, one row each (a header row besides).
  await expect(table.getByRole("row")).toHaveCount(10);
  await expect(table.getByRole("rowheader", { name: /^umami/ }).first()).toBeVisible();
  await expect(
    page.getByRole("link", { name: "github.com/K-sau07/crosslayerbench" }),
  ).toHaveAttribute("href", "https://github.com/K-sau07/crosslayerbench");
  await expect(
    page.getByRole("contentinfo").getByRole("link", { name: "Benchmarks" }),
  ).toBeVisible();
});
