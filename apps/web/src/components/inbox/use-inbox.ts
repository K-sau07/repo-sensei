"use client";

import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { api } from "@/lib/api/client";
import type { InboxPage } from "@/lib/api/inbox";
import { followStream } from "@/lib/event-stream";
import { queryKeys } from "@/lib/query/keys";

const PAGE = 50;

/** The signed-in person's inbox in ``org``, newest first, a page at a time (``unread``: only
 * what they haven't read). */
export function useInbox(org: string, unread: boolean) {
  return useInfiniteQuery({
    queryKey: queryKeys.inboxList(org, unread),
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam, signal }) => {
      const { data } = await api.GET("/api/v1/orgs/{org}/inbox", {
        params: {
          path: { org },
          query: { unread, limit: PAGE, ...(pageParam ? { before: pageParam } : {}) },
        },
        signal,
      });
      return data as InboxPage;
    },
    getNextPageParam: (last) => last.next_cursor ?? null,
  });
}

/** How many of their items in ``org`` are unread (the sidebar's badge). */
export function useInboxUnread(org: string | null) {
  return useQuery({
    queryKey: queryKeys.inboxUnread(org ?? ""),
    enabled: Boolean(org),
    queryFn: async ({ signal }) => {
      const { data } = await api.GET("/api/v1/orgs/{org}/inbox", {
        params: { path: { org: org ?? "" }, query: { unread: true, limit: 1 } },
        signal,
      });
      return (data as InboxPage).unread;
    },
  });
}

/** Mark some items read (``ids``), or all of them; the inbox and its badge read again. */
export function useInboxRead(org: string) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  const read = async (ids?: string[]) => {
    setBusy(true);
    try {
      await api.POST("/api/v1/orgs/{org}/inbox/read", {
        params: { path: { org } },
        body: ids ? { ids } : {},
      });
    } finally {
      setBusy(false);
      await queryClient.invalidateQueries({ queryKey: queryKeys.inbox(org) });
    }
  };
  return { read, busy };
}

interface Arrived {
  type: string; // "inbox.new"
  kind: string;
  key: string;
  title: string;
  link: string | null;
}

/** Follow the person's inbox in ``org`` (one stream per tab): each new item refreshes the
 * inbox and its badge, and is shown for a moment, with a way to open it. */
export function useInboxEvents(org: string | null) {
  const queryClient = useQueryClient();
  const router = useRouter();
  useEffect(() => {
    if (!org || typeof EventSource === "undefined") return;
    let lastId: string | null = null;
    const onEvent = (message: MessageEvent<string>) => {
      let event: Arrived;
      try {
        event = JSON.parse(message.data) as Arrived;
      } catch {
        return;
      }
      if (event.type !== "inbox.new") return;
      if (message.lastEventId) lastId = message.lastEventId;
      void queryClient.invalidateQueries({ queryKey: queryKeys.inbox(org) });
      const link = event.link?.startsWith("/") && !event.link.startsWith("//") ? event.link : null;
      toast(event.title, {
        id: `inbox:${event.key}`,
        ...(link
          ? {
              action: {
                label: "Open",
                onClick: () => {
                  router.push(link as never);
                },
              },
            }
          : {}),
      });
    };
    return followStream(`/api/v1/orgs/${encodeURIComponent(org)}/inbox/events`, {
      types: ["inbox.new"],
      onEvent,
      onLive: () => undefined,
      lastId: () => lastId,
    });
  }, [org, queryClient, router]);
}
