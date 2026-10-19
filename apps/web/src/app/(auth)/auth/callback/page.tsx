import type { Metadata } from "next";
import { Suspense } from "react";

import { SignInCallback } from "@/components/auth/sign-in-callback";

export const metadata: Metadata = { title: "Signing in" };

export default function AuthCallbackPage() {
  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <Suspense>
        <SignInCallback />
      </Suspense>
    </div>
  );
}
