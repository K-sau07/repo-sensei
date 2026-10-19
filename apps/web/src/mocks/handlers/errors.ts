import { HttpResponse } from "msw";

/** The API's error envelope, so mocked failures look exactly like real ones. */
export function apiError(status: number, code: string, message: string, details?: unknown) {
  return HttpResponse.json(
    { error: { code, message, request_id: "mock-request-id", ...(details ? { details } : {}) } },
    { status, headers: { "X-Request-Id": "mock-request-id" } },
  );
}
