"use client";

/**
 * The question box (4.10): ⌘↵ (Ctrl↵) sends, Stop ends an answer being written, Fast or Deep
 * (the deep model, for questions across the repository). What is typed is kept as a draft per
 * conversation (it survives a reload and a sign-in again) until it is sent. Keyed by its draft
 * where it is used, so another conversation's box starts from that one's draft.
 */
import { ArrowUp, Square } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { keepDraft, readDraft, type AskMode } from "@/lib/api/ask";

export function Composer({
  draftKey,
  busy,
  mode,
  onMode,
  onSend,
  onStop,
}: {
  draftKey: string;
  busy: boolean;
  mode: AskMode;
  onMode: (mode: AskMode) => void;
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  // Its draft, read once: the page gives each conversation's box a key of its own.
  const [text, setText] = useState(() => readDraft(draftKey));

  function change(value: string) {
    setText(value);
    keepDraft(draftKey, value);
  }

  function send() {
    const question = text.trim();
    if (!question || busy) return;
    onSend(question);
    change("");
  }

  return (
    <form
      className="bg-surface flex flex-col gap-2 rounded-lg border border-border p-2"
      onSubmit={(event) => {
        event.preventDefault();
        send();
      }}
    >
      <Textarea
        aria-label="Ask about this repository"
        placeholder="Ask how something works, where it lives, what calls it…"
        value={text}
        rows={3}
        className="resize-none border-0 shadow-none focus-visible:ring-0"
        onChange={(event) => {
          change(event.target.value);
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            send();
          }
        }}
      />
      <div className="flex items-center justify-between">
        <ToggleGroup
          aria-label="Mode"
          value={[mode]}
          onValueChange={(value: string[]) => {
            const next = value[0];
            if (next === "fast" || next === "deep") onMode(next);
          }}
        >
          <ToggleGroupItem value="fast" size="sm">
            Fast
          </ToggleGroupItem>
          <ToggleGroupItem value="deep" size="sm">
            Deep
          </ToggleGroupItem>
        </ToggleGroup>
        {busy ? (
          <Button type="button" size="sm" variant="outline" onClick={onStop}>
            <Square aria-hidden className="size-3.5" /> Stop
          </Button>
        ) : (
          <Button type="submit" size="sm" disabled={!text.trim()}>
            <ArrowUp aria-hidden className="size-3.5" /> Ask
          </Button>
        )}
      </div>
    </form>
  );
}
