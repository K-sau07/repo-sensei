import type { Metadata } from "next";

import { ComingSoon } from "@/components/coming-soon";

export const metadata: Metadata = { title: "Internal", robots: { index: false } };

export default function InternalPage() {
  return <ComingSoon title="Internal" />;
}
