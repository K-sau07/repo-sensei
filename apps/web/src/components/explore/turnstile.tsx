"use client";

/**
 * Cloudflare Turnstile's check (10.2): that the question on a public page comes from a person.
 * Its script is loaded once, on the first page that shows the check, and draws the check in a
 * frame of its own (explicit rendering). A token is good once, for five minutes: each question
 * spends one, and the check is drawn again for the next (`round`).
 */
import { useEffect, useRef } from "react";

const SCRIPT = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

interface TurnstileApi {
  render(
    element: HTMLElement,
    options: {
      sitekey: string;
      action?: string;
      size?: "normal" | "flexible" | "compact";
      callback: (token: string) => void;
      "expired-callback": () => void;
      "error-callback": () => void;
    },
  ): string;
  reset(widget: string): void;
  remove(widget: string): void;
}

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

let loading: Promise<TurnstileApi> | null = null;

function loaded(): Promise<TurnstileApi> {
  if (window.turnstile) return Promise.resolve(window.turnstile);
  loading ??= new Promise<TurnstileApi>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = SCRIPT;
    script.async = true;
    script.onload = () => {
      if (window.turnstile) resolve(window.turnstile);
      else reject(new Error("Turnstile didn't load."));
    };
    script.onerror = () => {
      loading = null; // tried again on the next page that shows it
      reject(new Error("Turnstile didn't load."));
    };
    document.head.append(script);
  });
  return loading;
}

export function Turnstile({
  siteKey,
  round,
  onToken,
}: {
  siteKey: string;
  /** Changed after each question: its token is spent, so the check starts again. */
  round: number;
  /** A token once the check passes; null when it lapses or fails. */
  onToken: (token: string | null) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const widget = useRef<string | null>(null);
  const told = useRef(onToken);
  useEffect(() => {
    told.current = onToken;
  });

  useEffect(() => {
    let gone = false;
    loaded()
      .then((api) => {
        if (gone || box.current === null) return;
        widget.current = api.render(box.current, {
          sitekey: siteKey,
          action: "ask",
          size: "flexible",
          callback: (token) => {
            told.current(token);
          },
          "expired-callback": () => {
            told.current(null);
          },
          "error-callback": () => {
            told.current(null);
          },
        });
      })
      .catch(() => {
        told.current(null);
      });
    return () => {
      gone = true;
      if (widget.current !== null) window.turnstile?.remove(widget.current);
      widget.current = null;
    };
  }, [siteKey]);

  useEffect(() => {
    if (round === 0 || widget.current === null) return;
    told.current(null);
    window.turnstile?.reset(widget.current);
  }, [round]);

  return <div ref={box} data-testid="turnstile" className="min-h-[65px]" />;
}
