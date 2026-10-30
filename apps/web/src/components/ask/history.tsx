"use client";

/**
 * The Ask tab's history (4.11): the user's conversations about the repository, the latest
 * first, found by their title; each renamed in place or deleted (after asking).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { cn } from "cn";
import { Pencil, Plus, Search, Trash2 } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { useRepoSlug } from "@/components/repo/use-repo";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { titleOr } from "@/lib/api/ask";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { queryKeys } from "@/lib/query/keys";
import { repoPath } from "@/lib/routes";

const SEARCH_MS = 200;

interface Listed {
  id: string;
  title: string;
}

export function useHistory(q: string) {
  const { org, repo } = useRepoSlug();
  return useQuery({
    queryKey: [...queryKeys.conversations(org, repo), q],
    queryFn: async ({ signal }) => {
      const { data } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/conversations", {
        params: { path: { org, repo }, query: { limit: 50, ...(q ? { q } : {}) } },
        signal,
      });
      if (!data) throw new Error("no conversations");
      return data.conversations;
    },
  });
}

export function ConversationHistory({
  current,
  onDeleted,
}: {
  current: string | null;
  onDeleted: (id: string) => void;
}) {
  const { org, repo } = useRepoSlug();
  const [typed, setTyped] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => {
      setQ(typed.trim());
    }, SEARCH_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [typed]);
  const listed = useHistory(q);
  const [deleting, setDeleting] = useState<Listed | null>(null);
  const queryClient = useQueryClient();
  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: queryKeys.conversations(org, repo) });

  const remove = async (gone: Listed) => {
    try {
      await api.DELETE("/api/v1/orgs/{org}/conversations/{conversation_id}", {
        params: { path: { org, conversation_id: gone.id } },
      });
      toast.success("Conversation deleted");
      onDeleted(gone.id);
      await refresh();
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "It couldn't be deleted.");
    }
  };

  return (
    <nav aria-label="Conversations" className="flex h-full flex-col border-r border-border">
      <div className="flex items-center gap-2 p-3">
        <div className="relative flex-1">
          <Search
            aria-hidden
            className="absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-fg-muted"
          />
          <Input
            aria-label="Find a conversation"
            placeholder="Find a conversation"
            value={typed}
            onChange={(event) => {
              setTyped(event.target.value);
            }}
            className="h-8 pl-7 text-xs"
          />
        </div>
        <Link
          href={repoPath(org, repo, "ask")}
          aria-label="New conversation"
          className={buttonVariants({ size: "icon-sm", variant: "ghost" })}
        >
          <Plus aria-hidden />
        </Link>
      </div>
      <ul className="flex-1 overflow-auto px-2 pb-3" data-testid="history">
        {listed.data?.map((c) => (
          <Row
            key={c.id}
            conversation={c}
            current={c.id === current}
            onRenamed={() => void refresh()}
            onDelete={() => {
              setDeleting(c);
            }}
          />
        ))}
        {listed.data?.length === 0 ? (
          <li className="px-2 py-1 text-xs text-fg-muted">
            {q ? "No conversation is titled like that." : "No conversations yet."}
          </li>
        ) : null}
      </ul>
      <AlertDialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this conversation?</AlertDialogTitle>
            <AlertDialogDescription>
              “{titleOr(deleting?.title, "Untitled")}”, its answers and any link it was shared by
              are gone for good.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={() => {
                const gone = deleting;
                setDeleting(null);
                if (gone) void remove(gone);
              }}
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </nav>
  );
}

function Row({
  conversation,
  current,
  onRenamed,
  onDelete,
}: {
  conversation: Listed;
  current: boolean;
  onRenamed: () => void;
  onDelete: () => void;
}) {
  const { org, repo } = useRepoSlug();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(conversation.title);
  const name = titleOr(conversation.title, "Untitled");
  const field = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (editing) field.current?.select(); // renaming starts with the title picked
  }, [editing]);

  const rename = async () => {
    setEditing(false);
    const next = title.trim();
    if (!next || next === conversation.title) {
      setTitle(conversation.title);
      return;
    }
    try {
      await api.PATCH("/api/v1/orgs/{org}/conversations/{conversation_id}", {
        params: { path: { org, conversation_id: conversation.id } },
        body: { title: next },
      });
      onRenamed();
    } catch (error) {
      setTitle(conversation.title);
      toast.error(error instanceof ApiError ? error.message : "It couldn't be renamed.");
    }
  };

  if (editing) {
    return (
      <li className="py-0.5">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void rename();
          }}
        >
          <Input
            ref={field}
            aria-label="Conversation title"
            value={title}
            maxLength={200}
            onChange={(event) => {
              setTitle(event.target.value);
            }}
            onBlur={() => void rename()}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                setTitle(conversation.title);
                setEditing(false);
              }
            }}
            className="h-7 text-xs"
          />
        </form>
      </li>
    );
  }
  return (
    <li
      className={cn(
        "group flex items-center gap-1 rounded-sm pr-1 hover:bg-muted",
        current && "bg-muted",
      )}
    >
      <Link
        href={repoPath(org, repo, `ask/${conversation.id}`)}
        aria-current={current ? "page" : undefined}
        className="min-w-0 flex-1 truncate px-2 py-1 text-sm"
      >
        {name}
      </Link>
      <Button
        size="icon-xs"
        variant="ghost"
        aria-label={`Rename ${name}`}
        className="opacity-0 group-focus-within:opacity-100 group-hover:opacity-100"
        onClick={() => {
          setEditing(true);
        }}
      >
        <Pencil aria-hidden />
      </Button>
      <Button
        size="icon-xs"
        variant="ghost"
        aria-label={`Delete ${name}`}
        className="opacity-0 group-focus-within:opacity-100 group-hover:opacity-100"
        onClick={onDelete}
      >
        <Trash2 aria-hidden />
      </Button>
    </li>
  );
}
