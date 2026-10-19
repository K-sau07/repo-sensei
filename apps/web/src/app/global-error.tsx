"use client";

import "./globals.css";

/**
 * Last resort when the root layout itself fails. It replaces the whole document, so it carries
 * its own <html> and avoids every provider (they may be what broke).
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en" data-theme="dark">
      <body className="flex min-h-dvh items-center justify-center bg-bg p-8 font-sans text-fg">
        <main className="flex max-w-md flex-col items-start gap-3">
          <h1 className="text-lg font-semibold">repo-sensei hit an error</h1>
          <p className="text-sm text-fg-muted">
            Reload to try again. If it keeps happening, share the reference below with support.
          </p>
          {error.digest && (
            <p className="text-xs text-fg-muted">
              Reference <code className="font-mono">{error.digest}</code>
            </p>
          )}
          <button
            type="button"
            onClick={reset}
            className="h-control-md rounded-sm bg-brand px-3 text-sm font-medium text-brand-fg"
          >
            Try again
          </button>
        </main>
      </body>
    </html>
  );
}
