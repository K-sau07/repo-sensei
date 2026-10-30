import type { Metadata } from "next";
import { Suspense } from "react";

import { AskPage } from "@/components/ask/ask-page";

export const metadata: Metadata = { title: "Ask" };

export default function RepoAskPage() {
  return (
    <Suspense>
      <AskPage />
    </Suspense>
  );
}
