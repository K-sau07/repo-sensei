import type { Metadata, Viewport } from "next";

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

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // next-themes sets data-theme before hydration; the density preference arrives with settings.
    <html lang="en" className={fontVariables} data-density="comfortable" suppressHydrationWarning>
      <head>
        {/* Restores the sidebar width and rail before first paint, so the shell never shifts. */}
        <script dangerouslySetInnerHTML={{ __html: SHELL_BOOT_SCRIPT }} />
      </head>
      <body className="flex min-h-dvh flex-col">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
