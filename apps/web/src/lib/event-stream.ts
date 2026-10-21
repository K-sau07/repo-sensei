/**
 * Following a server-sent event stream that must not stop: `EventSource` reconnects by itself
 * (sending `Last-Event-ID`) after a dropped connection, but gives up for good when a reconnect
 * gets an error response (a deploy's 502, say). Then this opens a new one after a pause, longer
 * each time, resuming after the last event seen (`?last_event_id=`), which the API accepts as
 * well as the header.
 */

/** Pauses before reopening, in ms; the last one repeats. */
export const RETRY_MS = [1000, 2000, 5000, 10_000, 30_000];
/** After this many failed reopens in a row, stop (the stream is gone, not down). */
export const MAX_RETRIES = 20;

export interface StreamHandlers {
  /** Event types to listen to (unnamed `message` events are always delivered). */
  types: readonly string[];
  onEvent: (message: MessageEvent<string>) => void;
  onLive: (live: boolean) => void;
  /** Where to resume when reopening: the id of the last event handled. */
  lastId: () => string | null;
  /** True once nothing more should come (the run ended): no reopening then. */
  finished?: () => boolean;
}

/** Open `url` and keep it open; returns the function that closes it for good. */
export function followStream(url: string, handlers: StreamHandlers): () => void {
  let source: EventSource | null = null;
  let closed = false;
  let failures = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const stop = () => {
    source?.close();
    source = null;
  };

  const open = () => {
    timer = undefined;
    if (closed || handlers.finished?.()) return;
    const last = handlers.lastId();
    const target = last
      ? `${url}${url.includes("?") ? "&" : "?"}last_event_id=${encodeURIComponent(last)}`
      : url;
    const current = new EventSource(target);
    source = current;
    current.onopen = () => {
      failures = 0;
      handlers.onLive(true);
    };
    current.onerror = () => {
      handlers.onLive(false);
      // CONNECTING: the browser is retrying by itself. CLOSED: it gave up.
      if (current.readyState !== EventSource.CLOSED || closed) return;
      stop();
      if (handlers.finished?.() || failures >= MAX_RETRIES) return;
      const wait = RETRY_MS[Math.min(failures, RETRY_MS.length - 1)] ?? 30_000;
      failures += 1;
      timer = setTimeout(open, wait);
    };
    for (const type of handlers.types) current.addEventListener(type, handlers.onEvent);
    current.addEventListener("message", handlers.onEvent);
  };

  // In mock mode the stream must start after the mock worker does, or it reaches the dev
  // server's proxy instead. The env check is inline so real builds drop this branch.
  if (process.env.NEXT_PUBLIC_MOCKS === "1") {
    void import("@/mocks/start").then(({ startMocks }) => startMocks()).then(open);
  } else {
    open();
  }

  return () => {
    closed = true;
    clearTimeout(timer);
    stop();
  };
}
