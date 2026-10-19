/**
 * The typed API client. The browser always calls its own origin (`/api/v1/...`): Next proxies to
 * FastAPI in development and the load balancer routes it in deployed environments.
 */
import createFetchClient, { type Middleware } from "openapi-fetch";
import createQueryClient from "openapi-react-query";

import { emitAuthExpired } from "./auth-events";
import { ApiError, apiErrorFromResponse, networkError } from "./errors";
import type { paths } from "./schema";

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
// Must match the API's cookie names (__Host- prefixed when served over HTTPS).
const CSRF_COOKIES = ["__Host-csrf", "csrf"];

/** Read the CSRF cookie the API set at sign-in (not HttpOnly, by design). */
export function readCsrfCookie(cookieHeader: string): string | null {
  for (const part of cookieHeader.split(";")) {
    const [rawName, ...rest] = part.trim().split("=");
    if (rawName && CSRF_COOKIES.includes(rawName)) return decodeURIComponent(rest.join("="));
  }
  return null;
}

export interface ApiClientOptions {
  baseUrl?: string;
  fetch?: typeof globalThis.fetch;
  /** Where the CSRF token comes from; the document's cookies by default. */
  getCsrfToken?: () => string | null;
  newRequestId?: () => string;
}

export function apiMiddleware({
  getCsrfToken = () => (typeof document === "undefined" ? null : readCsrfCookie(document.cookie)),
  newRequestId = () => crypto.randomUUID(),
}: Pick<ApiClientOptions, "getCsrfToken" | "newRequestId"> = {}): Middleware {
  return {
    onRequest({ request }) {
      request.headers.set("X-Request-Id", newRequestId());
      if (!SAFE_METHODS.has(request.method)) {
        const token = getCsrfToken();
        if (token) request.headers.set("X-CSRF-Token", token);
      }
      return request;
    },
    async onResponse({ response }) {
      if (response.ok) return response;
      if (response.status === 401) emitAuthExpired();
      throw await apiErrorFromResponse(response);
    },
    onError({ error }) {
      return error instanceof ApiError ? error : networkError(error);
    },
  };
}

export function createApiClient({ baseUrl = "", fetch, ...rest }: ApiClientOptions = {}) {
  const client = createFetchClient<paths>({
    baseUrl,
    credentials: "same-origin",
    ...(fetch ? { fetch } : {}),
  });
  client.use(apiMiddleware(rest));
  return client;
}

export const api = createApiClient();

/** React Query hooks over `api`: `$api.useQuery("get", "/api/v1/me")`. */
export const $api = createQueryClient(api);
