"use client";

import { useState } from "react";

import { signOut } from "@/components/shell/sign-out";
import { Button } from "@/components/ui/button";

/** /logout asks before signing out, so a link from another site can't sign anyone out. */
export function SignOutNow() {
  const [state, setState] = useState<"idle" | "busy" | "failed">("idle");
  const run = () => {
    setState("busy");
    void signOut().then((done) => {
      if (!done) setState("failed");
    });
  };
  return (
    <div className="flex flex-col items-center gap-3 text-sm">
      {state === "failed" ? (
        <p role="alert">We couldn&rsquo;t sign you out. You&rsquo;re still signed in.</p>
      ) : (
        <p>Sign out of repo-sensei on this device?</p>
      )}
      <Button disabled={state === "busy"} onClick={run}>
        {state === "busy" ? "Signing out…" : state === "failed" ? "Try again" : "Sign out"}
      </Button>
    </div>
  );
}
