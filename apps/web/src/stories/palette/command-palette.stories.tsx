import type { Decorator, Meta, StoryObj } from "@storybook/nextjs-vite";
import { delay, http } from "msw";
import { expect, screen, userEvent, waitFor, within } from "storybook/test";

import { CommandBar } from "@/components/shell/command-bar";
import { useShellStore } from "@/components/shell/shell-store";
import { clearHistory } from "@/lib/palette/history";
import { apiError, handlers } from "@/mocks/handlers";

/** The palette as ⌘K opens it, in the code browser of the mock repository. */
function Palette() {
  return <CommandBar />;
}

const opened: Decorator = (Story) => {
  clearHistory();
  useShellStore.setState({ commandOpen: true });
  return <Story />;
};

const navigation = (pathname: string, segments: [string, string | string[]][]) => ({
  appDirectory: true,
  navigation: { pathname, segments },
});

const IN_REPO = navigation("/acme/api/code", [
  ["org", "acme"],
  ["repo", "api"],
]);

const meta = {
  title: "Shell/Command palette",
  component: Palette,
  decorators: [opened],
  parameters: { bare: true, nextjs: IN_REPO, msw: handlers },
} satisfies Meta<typeof Palette>;

export default meta;
type Story = StoryObj<typeof meta>;

const palette = () => screen.findByRole("dialog", { name: "Command palette" });
// (By role alone: testing-library stops at cmdk's empty aria-labelledby; browsers name it "Command".)
const input = () => screen.findByRole("combobox");
const group = async (name: string) => {
  const heading = await within(await palette()).findByText(name, {
    selector: "[cmdk-group-heading]",
  });
  const element = heading.closest<HTMLElement>("[cmdk-group]");
  if (!element) throw new Error(`no group ${name}`);
  return element;
};
const selectedOption = async () => {
  const dialog = await palette();
  return waitFor(() => {
    const option = dialog.querySelector<HTMLElement>('[role="option"][aria-selected="true"]');
    if (!option) throw new Error("nothing selected");
    return option;
  });
};

/** Opened with nothing typed: pages and the repository's tabs, actions, and Ask. */
export const Empty: Story = {
  play: async () => {
    await expect(await input()).toHaveFocus();
    const go = await group("Go");
    // The repository's tabs, once its index state says they're unlocked.
    await expect(await within(go).findByRole("option", { name: /^Code/ })).toBeVisible();
    await expect(within(go).getByRole("option", { name: /^Search/ })).toBeVisible();
    await expect(within(go).getByRole("option", { name: /Home/ })).toBeVisible();
    const actions = await group("Do");
    await expect(within(actions).getByRole("option", { name: /Reindex api/ })).toBeVisible();
    const ask = await group("Ask");
    await expect(within(ask).getByRole("option", { name: /Ask a question/ })).toBeVisible();
    // The first result is selected, and the input points at it.
    await waitFor(async () => {
      const first = await selectedOption();
      await expect(first).toHaveTextContent("Search");
      await expect(await input()).toHaveAttribute("aria-activedescendant", first.id);
    });
  },
};

/** Typing matches pages and actions on the spot, with what matched emphasised. */
export const TypingLocalResults: Story = {
  play: async () => {
    await userEvent.type(await input(), "inbx");
    const option = await selectedOption();
    await expect(option).toHaveTextContent("Inbox");
    await expect([...option.querySelectorAll("mark")].map((mark) => mark.textContent)).toEqual([
      "Inb",
      "x",
    ]);
    await expect(
      within(await group("Ask")).getByRole("option", { name: /Ask “inbx”/ }),
    ).toBeVisible();
  },
};

/** Files and symbols from the API join in, ranked with the rest. */
export const ServerResults: Story = {
  play: async () => {
    await userEvent.type(await input(), "login_acc");
    const go = await group("Go");
    const symbol = await within(go).findByRole("option", { name: /^login_access_token/ });
    await expect(symbol).toHaveTextContent("function · backend/app/api/routes/login.py:23");
    await expect(symbol).toHaveTextContent("Symbol");
    await userEvent.clear(await input());
    await userEvent.type(await input(), "items.py");
    const file = await within(await group("Go")).findByRole("option", {
      name: /items\.py.*backend\/app\/api\/routes/,
    });
    await expect(file).toHaveTextContent("File");
    await waitFor(async () => {
      await expect(await selectedOption()).toBe(file);
    });
  },
};

/** The symbols search fails: one muted row says so, and files still come. */
export const SymbolsError: Story = {
  parameters: {
    msw: [
      http.get("*/api/v1/orgs/:org/repos/:repo/symbols", () =>
        apiError(503, "service_unavailable", "The API is restarting."),
      ),
      ...handlers,
    ],
  },
  play: async () => {
    await userEvent.type(await input(), "login");
    const go = await group("Go");
    const notice = await within(go).findByRole("option", { name: "Couldn't search symbols" });
    await expect(notice).toHaveAttribute("aria-disabled", "true");
    await expect(
      await within(go).findByRole("option", { name: /login\.py.*backend\/app\/api\/routes/ }),
    ).toBeVisible();
  },
};

/** No symbols before the first index: said plainly, not as an error. */
export const NotIndexed: Story = {
  parameters: {
    msw: [
      http.get("*/api/v1/orgs/:org/repos/:repo/symbols", () =>
        apiError(409, "not_indexed", "Symbols appear once this repository is indexed."),
      ),
      ...handlers,
    ],
  },
  play: async () => {
    await userEvent.type(await input(), "login");
    await expect(
      await within(await group("Go")).findByRole("option", {
        name: "Symbols appear after indexing",
      }),
    ).toHaveAttribute("aria-disabled", "true");
  },
};

/** Actions that aren't ready are listed with why, and can't be picked. */
export const DisabledAction: Story = {
  play: async () => {
    await userEvent.type(await input(), "trace this");
    const trace = await within(await group("Do")).findByRole("option", { name: /Trace this/ });
    await expect(trace).toHaveAttribute("aria-disabled", "true");
    await expect(trace).toHaveTextContent("Tracing is coming soon");
    // The selection skips it.
    await expect(await selectedOption()).not.toBe(trace);
  },
};

/** Until the repository's runs have loaded, Reindex can't know whether one is under way. */
export const ReindexChecking: Story = {
  parameters: {
    msw: [
      http.get("*/api/v1/orgs/:org/repos/:repo/index-jobs", async () => {
        await delay("infinite");
      }),
      ...handlers,
    ],
  },
  play: async () => {
    await userEvent.type(await input(), "reindex");
    const reindex = await within(await group("Do")).findByRole("option", { name: /Reindex api/ });
    await expect(reindex).toHaveAttribute("aria-disabled", "true");
    await expect(reindex).toHaveTextContent("Checking…");
  },
};

/** Outside a repository: no file or symbol search, and Ask goes to the repository there is. */
export const InTheOrg: Story = {
  parameters: { nextjs: navigation("/acme", [["org", "acme"]]) },
  play: async () => {
    await userEvent.type(await input(), "api");
    await expect(
      await within(await group("Go")).findByRole("option", { name: /^api.*acme-gh\/api/ }),
    ).toBeVisible();
    await expect(
      within(await group("Ask")).getByRole("option", { name: /Ask “api”.*about api/ }),
    ).toBeVisible();
  },
};

export const Light: Story = { globals: { theme: "light" } };
