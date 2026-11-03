"use client";

import { useEffect, useRef, useState } from "react";

import { citationsOf, type AskMessage } from "@/lib/api/ask";

/** What a screen reader is told once an answer is written (not each streamed word): for an
 * `aria-live` element apart from the thread. */
export function useAnnouncement(latest: AskMessage | undefined, streaming: boolean): string {
  const [said, setSaid] = useState("");
  const was = useRef(false);
  useEffect(() => {
    if (was.current && !streaming && latest) {
      const n = citationsOf(latest).length;
      setSaid(`Answer ready, ${n} citation${n === 1 ? "" : "s"}`);
    }
    was.current = streaming;
  }, [streaming, latest]);
  return said;
}
