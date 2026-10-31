"use client";

/**
 * The Ask page's chat: `useChat` over the Ask endpoint (lib/api/ask.ts), in the conversation
 * the page is on (none: a new one, which the stream names as it starts), in the mode picked.
 * An existing conversation's messages are read first and shown as the thread so far.
 */
import { useChat } from "@ai-sdk/react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { useViewRef } from "@/components/repo/use-ref";
import { useRepoSlug } from "@/components/repo/use-repo";
import { askTransport, type AskMessage, type AskMode, type ScopeData } from "@/lib/api/ask";
import { api } from "@/lib/api/client";
import { queryKeys } from "@/lib/query/keys";

export interface StoredMessage {
  id: string;
  role: "user" | "assistant";
  parts: AskMessage["parts"];
  status: "streaming" | "done" | "failed";
}

export function useConversation(conversationId: string | null) {
  const { org } = useRepoSlug();
  return useQuery({
    queryKey: queryKeys.conversation(org, conversationId ?? ""),
    queryFn: async ({ signal }) => {
      const { data } = await api.GET("/api/v1/orgs/{org}/conversations/{conversation_id}", {
        params: { path: { org, conversation_id: conversationId ?? "" } },
        signal,
      });
      if (!data) throw new Error("no conversation");
      return data;
    },
    enabled: conversationId !== null,
    staleTime: Infinity, // the thread is the chat's from here on
  });
}

export function useConversations() {
  const { org, repo } = useRepoSlug();
  return useQuery({
    queryKey: queryKeys.conversations(org, repo),
    queryFn: async ({ signal }) => {
      const { data } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/conversations", {
        params: { path: { org, repo }, query: { limit: 10 } },
        signal,
      });
      if (!data) throw new Error("no conversations");
      return data.conversations;
    },
  });
}

export function useAsk(conversationId: string | null, onConversation: (id: string) => void) {
  const { org, repo } = useRepoSlug();
  const [mode, setMode] = useState<AskMode>("fast");
  const [ref] = useViewRef(); // asked at the branch the page is read at
  const announce = useRef(onConversation);
  // The conversation whose stored messages the thread shows (a new one's are the chat's own).
  const loaded = useRef<string | null>(null);
  useEffect(() => {
    announce.current = onConversation;
  });
  const transport = useMemo(() => askTransport({ org, repo }), [org, repo]);
  const queryClient = useQueryClient();
  // The history lists it (a new one), with its title (written as the answer is).
  const relist = () =>
    queryClient.invalidateQueries({ queryKey: queryKeys.conversations(org, repo) });
  const chat = useChat<AskMessage>({
    transport,
    onData: (part) => {
      if (part.type !== "data-conversation") return;
      const found = part.data.id;
      if (found !== loaded.current) {
        loaded.current = found; // its messages are this chat's: never read back over them
        announce.current(found);
        void relist();
      }
    },
    onFinish: () => {
      void relist();
      // Its title and sharing, for the header (its messages stay the chat's: see `loaded`).
      if (loaded.current !== null) {
        void queryClient.invalidateQueries({
          queryKey: queryKeys.conversation(org, loaded.current),
        });
      }
    },
  });
  const stored = useConversation(conversationId);
  const { setMessages, sendMessage } = chat;
  useEffect(() => {
    if (!stored.data || loaded.current === stored.data.id) return;
    loaded.current = stored.data.id;
    setMessages(
      stored.data.messages.map((m) => ({
        id: m.id,
        role: m.role,
        parts: m.parts as AskMessage["parts"],
      })),
    );
  }, [stored.data, setMessages]);
  // The conversation the stream named, until the page's own catches up with it.
  // A scoped question shows what it was asked about, as the stored one does.
  const ask = (text: string, scope: ScopeData | null = null) => {
    const body = { conversationId: conversationId ?? loaded.current, mode, scope, ref };
    if (scope === null) return sendMessage({ text }, { body });
    const parts: AskMessage["parts"] = [
      { type: "text", text },
      { type: "data-scope", id: "scope", data: scope },
    ];
    return sendMessage({ parts }, { body });
  };
  return {
    ...chat,
    ask,
    mode,
    setMode,
    stored: stored.data,
    loading: stored.isPending && conversationId !== null,
  };
}
