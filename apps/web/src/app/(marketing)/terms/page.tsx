import type { Metadata } from "next";

import { LongPage } from "@/components/marketing/long-page";
import Terms from "@/content/legal/terms.mdx";

export const metadata: Metadata = {
  title: "Terms",
  description: "The terms of using repo-sensei in early access, in plain words.",
  alternates: { canonical: "/terms" },
};

export default function TermsPage() {
  return (
    <LongPage title="Terms" updated="2026-11-10">
      <Terms />
    </LongPage>
  );
}
