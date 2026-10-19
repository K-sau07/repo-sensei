/**
 * The Content Security Policy, built per request around a fresh nonce.
 *
 * Scripts run only if they carry the nonce ('strict-dynamic' lets those scripts load the app's
 * chunks), so injected markup can't execute even where a bug lets it into the page. Styles allow
 * inline because React renders style attributes; style injection can't run code.
 */

export function newNonce(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return btoa(String.fromCharCode(...bytes));
}

export function contentSecurityPolicy(nonce: string, { dev }: { dev: boolean }): string {
  return [
    "default-src 'self'",
    // Next's dev overlay and React's dev tooling evaluate code; production never does.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: https://avatars.githubusercontent.com",
    "font-src 'self'",
    `connect-src 'self'${dev ? " ws:" : ""}`,
    // 'strict-dynamic' ignores 'self' for scripts, so workers (the dev mock API) need their own.
    "worker-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
    ...(dev ? [] : ["upgrade-insecure-requests"]),
  ].join("; ");
}
