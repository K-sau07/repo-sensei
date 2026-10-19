/** A tiny event bus for "the session is gone" (any API call answered 401). */

const target = new EventTarget();
const AUTH_EXPIRED = "auth:expired";

export function emitAuthExpired(): void {
  target.dispatchEvent(new Event(AUTH_EXPIRED));
}

export function onAuthExpired(listener: () => void): () => void {
  target.addEventListener(AUTH_EXPIRED, listener);
  return () => {
    target.removeEventListener(AUTH_EXPIRED, listener);
  };
}
