import { api } from "@/lib/api/client";

/** End this session, then leave with a full page load so no signed-in data stays in memory. */
export async function signOut(): Promise<void> {
  try {
    await api.POST("/api/v1/auth/logout");
  } finally {
    // A full load on purpose (not router.push): it drops every cached query and store.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.assign("/login");
  }
}
