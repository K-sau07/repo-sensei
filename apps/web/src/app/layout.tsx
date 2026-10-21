import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";

import { Providers } from "@/components/providers";
import { SHELL_BOOT_SCRIPT } from "@/components/shell/shell-store";
import { fontVariables } from "@/fonts";

import "./globals.css";

export const metadata: Metadata = {
  title: { default: "repo-sensei", template: "%s · repo-sensei" },
  description: "Understand any codebase, with answers you can verify.",
};

export const viewport: Viewport = {
  colorScheme: "dark light",
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#0c0d0f" },
    { media: "(prefers-color-scheme: light)", color: "#f9fafb" },
  ],
};

// Every page is rendered per request: the CSP nonce (src/proxy.ts) can't exist in prerendered
// HTML, and a strict policy is worth more here than static pages. `instant = false` allows the
// root to block on the request; segments below still stream behind their <Suspense> boundaries.
export const instant = false;

export default async function RootLayout({ children }: LayoutProps<"/">) {
  const nonce = (await headers()).get("x-nonce") ?? undefined;
  return (
    // next-themes sets data-theme before hydration; the density preference arrives with settings.
    <html lang="en" className={fontVariables} data-density="comfortable" suppressHydrationWarning>
      <head>
        {/* Restores the sidebar width and rail before first paint, so the shell never shifts. */}
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: SHELL_BOOT_SCRIPT }} />
      </head>
      <body className="flex min-h-dvh flex-col">
        <Providers nonce={nonce}>{children}</Providers>
      </body>
    </html>
  );
}
