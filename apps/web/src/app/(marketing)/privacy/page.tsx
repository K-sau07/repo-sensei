import type { Metadata } from "next";

import { LongPage } from "@/components/marketing/long-page";
import Privacy from "@/content/legal/privacy.mdx";

export const metadata: Metadata = {
  title: "Privacy",
  description: "What repo-sensei keeps about you and your code, why, and for how long.",
  alternates: { canonical: "/privacy" },
};

export default function PrivacyPage() {
  return (
    <LongPage title="Privacy" updated="2026-11-10">
      <Privacy />
    </LongPage>
  );
}
