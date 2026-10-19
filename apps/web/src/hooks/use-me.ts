import { $api } from "@/lib/api/client";

/** The signed-in user and their orgs (shared cache: every caller reads one request). */
export function useMe() {
  return $api.useQuery("get", "/api/v1/me");
}
