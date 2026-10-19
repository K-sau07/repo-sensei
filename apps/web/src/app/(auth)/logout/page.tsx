import type { Metadata } from "next";

import { SignOutNow } from "@/components/auth/sign-out-now";

export const metadata: Metadata = { title: "Signing out" };

export default function LogoutPage() {
  return (
    <div className="flex flex-1 items-center justify-center p-6 text-sm text-fg-muted">
      <SignOutNow />
    </div>
  );
}
