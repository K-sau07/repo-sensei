/**
 * Every failed API call becomes an ApiError, whether the server answered with its error envelope
 * (`{"error": {"code", "message", "request_id", "details"}}`), with something else, or not at all.
 */

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;
  readonly requestId: string | null;
  /** Seconds to wait before trying again (`Retry-After`, sent with a 429 or 503). */
  readonly retryAfter: number | null;

  constructor(options: {
    status: number;
    code: string;
    message: string;
    details?: unknown;
    requestId?: string | null;
    retryAfter?: number | null;
  }) {
    super(options.message);
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code;
    this.details = options.details;
    this.requestId = options.requestId ?? null;
    this.retryAfter = options.retryAfter ?? null;
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

/** `Retry-After` in seconds: a number of seconds, or an HTTP date. */
export function parseRetryAfter(value: string | null, now = Date.now()): number | null {
  if (value === null || value.trim() === "") return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, Math.ceil(seconds));
  const at = Date.parse(value);
  return Number.isNaN(at) ? null : Math.max(0, Math.ceil((at - now) / 1000));
}

/** Build an ApiError from a non-2xx response (reads the body). */
export async function apiErrorFromResponse(response: Response): Promise<ApiError> {
  const requestId = response.headers.get("x-request-id");
  const retryAfter = parseRetryAfter(response.headers.get("retry-after"));
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
      retryAfter,
    });
  }
  return new ApiError({
    status: response.status,
    code: `http_${response.status}`,
    message: response.statusText || `Request failed with status ${response.status}`,
    requestId,
    retryAfter,
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
