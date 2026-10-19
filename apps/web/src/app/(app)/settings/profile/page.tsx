import type { Metadata } from "next";

import { ComingSoon } from "@/components/coming-soon";

export const metadata: Metadata = { title: "Profile" };

export default function ProfilePage() {
  return <ComingSoon title="Profile" />;
}
