/**
 * Mock mode (NEXT_PUBLIC_MOCKS=1): serve the API from MSW in the browser so the UI runs without
 * the backend. Started once per page; React runs effects twice in development and MSW refuses a
 * second start.
 */
let starting: Promise<void> | null = null;

export function startMocks(): Promise<void> {
  starting ??= import("./browser")
    .then(({ worker }) => worker.start({ onUnhandledFrame: "bypass", quiet: true }))
    .then(() => undefined)
    .catch((error: unknown) => {
      // Carry on against the real API rather than hang, and say why.
      console.error("Mock API failed to start; using the real API.", error);
    });
  return starting;
}
