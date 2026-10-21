import type { Metadata } from "next";
import { Suspense } from "react";

import { CodeBrowser } from "@/components/code/code-browser";

export const metadata: Metadata = { title: "Code" };

export default function CodeLayout({ children }: LayoutProps<"/[org]/[repo]/code">) {
  return (
    <Suspense>
      <CodeBrowser>{children}</CodeBrowser>
    </Suspense>
  );
}
