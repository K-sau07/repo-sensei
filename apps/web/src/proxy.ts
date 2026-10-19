import { NextResponse, type NextRequest } from "next/server";

import { isPublicPath, loginRedirect, SESSION_COOKIES } from "@/lib/security/auth-paths";
import { contentSecurityPolicy, newNonce } from "@/lib/security/csp";

const DEV = process.env.NODE_ENV === "development";

/**
 * Runs before every page:
 * 1. Signed-out visitors to a signed-in page go to /login?next=… (an optimistic cookie check;
 *    authorization always happens in the API).
 * 2. Every page gets a fresh CSP nonce. Next reads it from the request's CSP header and puts it on
 *    its own scripts; the root layout passes it to ours.
 */
export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const hasSession = SESSION_COOKIES.some((name) => request.cookies.has(name));
  if (!hasSession && !isPublicPath(pathname)) {
    return NextResponse.redirect(new URL(loginRedirect(pathname, search), request.url));
  }

  const nonce = newNonce();
  const policy = contentSecurityPolicy(nonce, { dev: DEV });
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", policy);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", policy);
  return response;
}

export const config = {
  matcher: [
    {
      // Pages only: not the API, Next internals, static files or router prefetches.
      source: "/((?!api/|_next/static|_next/image|favicon.ico|.*\\.[a-z0-9]+$).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
