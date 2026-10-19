/**
 * Every failed API call becomes an ApiError, whether the server answered with its error envelope
 * (`{"error": {"code", "message", "request_id", "details"}}`), with something else, or not at all.
 */

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;
  readonly requestId: string | null;

  constructor(options: {
    status: number;
    code: string;
    message: string;
    details?: unknown;
    requestId?: string | null;
  }) {
    super(options.message);
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code;
    this.details = options.details;
    this.requestId = options.requestId ?? null;
  }

  /** 4xx: the request itself is wrong (or not allowed); retrying won't help. */
  get isClientError(): boolean {
    return this.status >= 400 && this.status < 500;
  }
}

interface ErrorEnvelope {
  error: { code: string; message: string; request_id?: string | null; details?: unknown };
}

function isEnvelope(body: unknown): body is ErrorEnvelope {
  if (typeof body !== "object" || body === null || !("error" in body)) return false;
  const { error } = body;
  return (
    typeof error === "object" &&
    error !== null &&
    typeof (error as { code?: unknown }).code === "string" &&
    typeof (error as { message?: unknown }).message === "string"
  );
}

/** Build an ApiError from a non-2xx response (reads the body). */
export async function apiErrorFromResponse(response: Response): Promise<ApiError> {
  const requestId = response.headers.get("x-request-id");
  let body: unknown = null;
  try {
    body = await response.clone().json();
  } catch {
    // Not JSON (a proxy error page, an empty body): fall through to the generic error.
  }
  if (isEnvelope(body)) {
    return new ApiError({
      status: response.status,
      code: body.error.code,
      message: body.error.message,
      details: body.error.details,
      requestId: body.error.request_id ?? requestId,
    });
  }
  return new ApiError({
    status: response.status,
    code: `http_${response.status}`,
    message: response.statusText || `Request failed with status ${response.status}`,
    requestId,
  });
}

export function networkError(cause: unknown): ApiError {
  const error = new ApiError({
    status: 0,
    code: "network_error",
    message: "Couldn't reach the server. Check your connection and try again.",
  });
  error.cause = cause;
  return error;
}
