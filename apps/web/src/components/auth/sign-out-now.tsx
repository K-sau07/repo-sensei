"use client";

import { useEffect } from "react";

import { signOut } from "@/components/shell/sign-out";

/** /logout: end the session as soon as the page loads. */
export function SignOutNow() {
  useEffect(() => {
    void signOut();
  }, []);
  return <p role="status">Signing you out…</p>;
}
