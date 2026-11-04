/** Each doc's MDX, by slug (loaded only by the docs route; ./docs-index.ts has their metadata). */
import type { ComponentType } from "react";

type Loaded = Promise<{ default: ComponentType }>;

export const DOC_PAGES: Record<string, () => Loaded> = {
  "getting-started": () => import("./docs/getting-started.mdx"),
  "connecting-repositories": () => import("./docs/connecting-repositories.mdx"),
  ask: () => import("./docs/ask.mdx"),
  trace: () => import("./docs/trace.mdx"),
  mcp: () => import("./docs/mcp.mdx"),
  cli: () => import("./docs/cli.mdx"),
  "security-model": () => import("./docs/security-model.mdx"),
  faq: () => import("./docs/faq.mdx"),
};
