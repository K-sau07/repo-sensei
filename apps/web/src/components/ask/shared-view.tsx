"use client";

/**
 * `/s/[token]` (4.11): a shared conversation, read-only, signed in or not. A viewer who can't
 * read the repository sees the answers' words and how sure they are; each citation is only its
 * number (the API sends nothing of the code). A link that was reset or stopped opens nothing.
 */
import { EyeOff, Lock } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";

import { DelayedSpinner } from "@/components/primitives/delayed-spinner";
import { chipLabel, type AskMessage, type CitationData } from "@/lib/api/ask";
import { $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";

import { Answer, Question } from "./answer";

export function SharedConversationView() {
  const { token } = useParams<{ token: string }>();
  const shared = $api.useQuery(
    "get",
    "/api/v1/s/{token}",
    { params: { path: { token } } },
    { retry: false },
  );

  if (shared.isPending) {
    return (
      <div className="flex flex-1 items-center justify-center p-6">
        <DelayedSpinner active label="Loading the conversation" />
      </div>
    );
  }
  if (shared.isError) {
    const gone = shared.error instanceof ApiError && shared.error.status === 404;
    return (
      <div className="mx-auto flex max-w-md flex-1 flex-col justify-center gap-2 p-6 text-center">
        <h1 className="text-lg font-semibold">
          {gone ? "This link doesn't open anything" : "This conversation couldn't be loaded"}
        </h1>
        <p className="text-sm text-fg-muted">
          {gone
            ? "It may have been reset or stopped by whoever shared it."
            : "Try again in a moment."}
        </p>
      </div>
    );
  }

  const { title, repo, redacted, messages } = shared.data;
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-6" data-testid="shared">
      <header className="flex flex-col gap-1 border-b border-border pb-4">
        <p className="text-xs text-fg-muted">
          Shared from repo-sensei · {repo ?? "a private repository"}
        </p>
        <h1 className="text-xl font-semibold">{title || "Untitled"}</h1>
      </header>
      {redacted ? (
        <p
          className="flex items-center gap-2 rounded-md border border-border bg-muted px-3 py-2 text-sm text-fg-muted"
          data-testid="redacted-note"
        >
          <EyeOff aria-hidden className="size-4 shrink-0" />
          <span>
            The code this cites is hidden: you can&apos;t read its repository.{" "}
            <Link href={`/login?next=${encodeURIComponent(`/s/${token}`)}`} className="underline">
              Sign in
            </Link>{" "}
            if you should.
          </span>
        </p>
      ) : null}
      {messages.map((m) => {
        const message: AskMessage = {
          id: m.id,
          role: m.role,
          parts: m.parts as AskMessage["parts"],
        };
        return m.role === "user" ? (
          <Question key={m.id} message={message} />
        ) : (
          <Answer key={m.id} message={message} streaming={false} chip={sharedChip} />
        );
      })}
    </div>
  );
}

/** A citation on a shared page: where it points (for a reader of the repository), or that it is
 * hidden. */
function sharedChip(citation: CitationData) {
  const hidden = citation.redacted === true || !citation.path;
  return (
    <span
      data-testid="citation-chip"
      className="mx-0.5 inline-flex h-5 items-center gap-1 rounded-sm border border-border bg-muted px-1.5 align-baseline font-mono text-[11px] leading-none text-fg-muted"
    >
      <span className="text-fg-faint">{citation.n}</span>
      {hidden ? (
        <>
          <Lock aria-hidden className="size-3" />
          <span className="sr-only">Citation hidden</span>
        </>
      ) : (
        <span title={`${citation.path}, lines ${citation.lines[0]}–${citation.lines[1]}`}>
          {chipLabel(citation.path, citation.lines)}
        </span>
      )}
    </span>
  );
}
