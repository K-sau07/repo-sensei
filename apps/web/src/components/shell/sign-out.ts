import { toast } from "sonner";

import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";

/**
 * End this session, then go to ``to`` (home) with a full page load so no signed-in data stays in
 * memory.
 * If the API can't be reached the session is still valid, so we stay put and say so rather than
 * look signed out (on a shared computer that matters). Never throws; returns whether it worked.
 */
export async function signOut(to: `/${string}` = "/"): Promise<boolean> {
  try {
    await api.POST("/api/v1/auth/logout");
  } catch (error) {
    // 401: the session was already gone, which is what we wanted.
    if (!(error instanceof ApiError && error.status === 401)) {
      toast.error("Couldn't sign you out. Check your connection and try again.");
      return false;
    }
  }
  // A full load on purpose (not router.push): it drops every cached query and store.
  window.location.assign(to);
  return true;
}
