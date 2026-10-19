import { NextResponse, type NextRequest } from "next/server";

/**
 * Runs before matching pages render. For now it passes everything through; the optimistic
 * session check for signed-in routes arrives with web auth (step 0.20).
 */
export function proxy(_request: NextRequest) {
  return NextResponse.next();
}

export const config = {
  // Pages only: skip the API, Next internals and static files.
  matcher: ["/((?!api/|_next/static|_next/image|favicon.ico|.*\\.[a-z0-9]+$).*)"],
};
