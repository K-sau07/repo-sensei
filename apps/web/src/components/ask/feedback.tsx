"use client";

/**
 * What the user says of an answer (4.11): helpful, or what was wrong with it (one of a few
 * kinds the evals read, with a note). Said again, it replaces what was said; said the same
 * again, it is taken back.
 */
import { cn } from "cn";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Textarea } from "@/components/ui/textarea";
import { FEEDBACK_KINDS, type FeedbackKind } from "@/lib/api/ask";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";

export function FeedbackButtons({
  messageId,
  given,
}: {
  messageId: string;
  given: FeedbackKind | null;
}) {
  const { org } = useRepoSlug();
  const [said, setSaid] = useState<FeedbackKind | null>(given);
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<FeedbackKind | null>(null);
  const [note, setNote] = useState("");
  const path = { org, message_id: messageId };

  const say = async (next: FeedbackKind | null, words = "") => {
    const before = said;
    setSaid(next);
    try {
      if (next === null) {
        await api.DELETE("/api/v1/orgs/{org}/messages/{message_id}/feedback", {
          params: { path },
        });
      } else {
        await api.POST("/api/v1/orgs/{org}/messages/{message_id}/feedback", {
          params: { path },
          body: { kind: next, note: words },
        });
        if (next !== "helpful") toast.success("Thanks: that goes into how answers are checked");
      }
    } catch (error) {
      setSaid(before);
      toast.error(error instanceof ApiError ? error.message : "That couldn't be saved.");
    }
  };

  const wrong = said !== null && said !== "helpful";
  return (
    <div className="flex items-center gap-1" data-testid="feedback">
      <Button
        size="icon-xs"
        variant="ghost"
        aria-label="Helpful"
        aria-pressed={said === "helpful"}
        className={cn(said === "helpful" && "text-ok")}
        onClick={() => void say(said === "helpful" ? null : "helpful")}
      >
        <ThumbsUp aria-hidden />
      </Button>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger
          render={
            <Button
              size="icon-xs"
              variant="ghost"
              aria-label="Something's wrong"
              aria-pressed={wrong}
              className={cn(wrong && "text-warn")}
            />
          }
        >
          <ThumbsDown aria-hidden />
        </PopoverTrigger>
        <PopoverContent
          align="start"
          aria-label="Feedback on this answer"
          className="w-80"
          data-testid="feedback-form"
        >
          <form
            className="flex flex-col gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (kind === null) return;
              setOpen(false);
              void say(kind, note.trim());
            }}
          >
            <p className="text-xs font-medium">What was wrong?</p>
            <div className="flex flex-wrap gap-1" role="group" aria-label="What was wrong">
              {FEEDBACK_KINDS.map((option) => (
                <button
                  key={option.kind}
                  type="button"
                  aria-pressed={kind === option.kind}
                  onClick={() => {
                    setKind(option.kind);
                  }}
                  className={cn(
                    "rounded-sm border border-border px-2 py-0.5 text-xs text-fg-muted hover:text-fg",
                    kind === option.kind && "border-primary text-fg",
                  )}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <Textarea
              aria-label="Anything to add"
              placeholder="Anything to add (optional)"
              value={note}
              maxLength={2000}
              onChange={(event) => {
                setNote(event.target.value);
              }}
              className="min-h-16 text-xs"
            />
            <div className="flex justify-end gap-2">
              {wrong ? (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setOpen(false);
                    void say(null);
                  }}
                >
                  Take back
                </Button>
              ) : null}
              <Button type="submit" size="sm" disabled={kind === null}>
                Send
              </Button>
            </div>
          </form>
        </PopoverContent>
      </Popover>
    </div>
  );
}
