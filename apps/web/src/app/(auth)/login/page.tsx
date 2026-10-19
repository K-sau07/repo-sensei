import type { Metadata } from "next";
import { Suspense } from "react";

import { ConstellationBackdrop } from "@/components/auth/constellation-backdrop";
import { LoginPanel } from "@/components/auth/login-panel";

export const metadata: Metadata = { title: "Sign in" };

export default function LoginPage() {
  return (
    <div className="relative flex flex-1 items-center justify-center overflow-hidden p-6">
      <ConstellationBackdrop />
      <div className="relative w-full max-w-sm rounded-xl border border-line bg-surface-1/90 p-8 shadow-dialog">
        {/* Reads ?next and ?error from the URL. */}
        <Suspense>
          <LoginPanel />
        </Suspense>
      </div>
    </div>
  );
}
