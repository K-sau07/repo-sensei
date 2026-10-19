import type { Metadata, Viewport } from "next";
import { Geist } from "next/font/google";

import { cn } from "@/lib/utils";

import "./globals.css";

const geist = Geist({ subsets: ["latin"], variable: "--font-sans" });

export const metadata: Metadata = {
  title: { default: "repo-sensei", template: "%s · repo-sensei" },
  description: "Understand any codebase, with answers you can verify.",
};

export const viewport: Viewport = {
  colorScheme: "dark",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={cn("dark h-full font-sans antialiased", geist.variable)}>
      <body className="flex min-h-full flex-col">{children}</body>
    </html>
  );
}
