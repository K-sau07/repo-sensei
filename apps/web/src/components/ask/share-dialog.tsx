"use client";

/**
 * Sharing a conversation (4.11): a link that opens it read-only, for anyone (its citations
 * hidden from a viewer who can't read the repository). Only the link's hash is kept, so the
 * link is shown once; "reset link" makes a new one (the old stops working), "stop sharing"
 * ends it.
 */
import { useQueryClient } from "@tanstack/react-query";
import { Check, Copy, Share2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { queryKeys } from "@/lib/query/keys";

export function ShareButton({
  conversationId,
  shared,
}: {
  conversationId: string;
  shared: boolean;
}) {
  const { org, repo } = useRepoSlug();
  const queryClient = useQueryClient();
  const [link, setLink] = useState<string | null>(null);
  const [on, setOn] = useState(shared);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const path = { org, conversation_id: conversationId };

  const settle = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.conversations(org, repo) }),
      queryClient.invalidateQueries({ queryKey: queryKeys.conversation(org, conversationId) }),
    ]);

  const share = async () => {
    setBusy(true);
    try {
      const { data } = await api.POST("/api/v1/orgs/{org}/conversations/{conversation_id}/share", {
        params: { path },
      });
      if (data) {
        setLink(`${window.location.origin}${data.path}`);
        setOn(true);
        setCopied(false);
      }
      await settle();
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "No link could be made.");
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    setBusy(true);
    try {
      await api.DELETE("/api/v1/orgs/{org}/conversations/{conversation_id}/share", {
        params: { path },
      });
      setLink(null);
      setOn(false);
      toast.success("The link no longer opens this conversation");
      await settle();
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Sharing couldn't be stopped.");
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    if (!link) return;
    await navigator.clipboard.writeText(link);
    setCopied(true);
  };

  return (
    <Dialog>
      <DialogTrigger render={<Button size="sm" variant="ghost" />}>
        <Share2 aria-hidden />
        Share
      </DialogTrigger>
      <DialogContent data-testid="share-dialog">
        <DialogHeader>
          <DialogTitle>Share this conversation</DialogTitle>
          <DialogDescription>
            Anyone with the link can read it. Its citations show only to people who can read this
            repository.
          </DialogDescription>
        </DialogHeader>
        {link ? (
          <div className="flex gap-2">
            <Input readOnly aria-label="Share link" value={link} className="font-mono text-xs" />
            <Button variant="outline" onClick={() => void copy()} aria-label="Copy link">
              {copied ? <Check aria-hidden /> : <Copy aria-hidden />}
            </Button>
          </div>
        ) : on ? (
          <p className="text-sm text-fg-muted">
            It is shared. The link was shown when it was made; reset it for a new one (the old one
            stops working).
          </p>
        ) : null}
        <DialogFooter>
          {on ? (
            <>
              <Button variant="ghost" disabled={busy} onClick={() => void stop()}>
                Stop sharing
              </Button>
              <Button variant="outline" disabled={busy} onClick={() => void share()}>
                Reset link
              </Button>
            </>
          ) : (
            <Button disabled={busy} onClick={() => void share()}>
              Create link
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
