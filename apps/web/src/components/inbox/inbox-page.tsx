"use client";

/**
 * `/[org]/inbox` (8.12.4): what the signed-in person was told in the org, newest first: plans
 * waiting for their approval (with the fix's risk), what finished (a pull request opened, a
 * repository indexed) and what failed. Unread first by default (`?show=all` for everything),
 * one group at a time (`?group=`). Triaged from the keyboard: `j`/`k` move, `enter` opens,
 * `a` opens a waiting approval's card, `e` marks the item done (read) and moves on.
 */
import { cn } from "cn";
import {
  CheckCheck,
  CircleAlert,
  CircleCheck,
  Gauge,
  GitPullRequest,
  Inbox,
  ShieldQuestion,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { parseAsStringLiteral, useQueryState } from "nuqs";
import { useRef } from "react";
import { toast } from "sonner";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { RiskBadge } from "@/components/fixes/fix-badges";
import { KeyHint } from "@/components/primitives/key-hint";
import { RelativeTime } from "@/components/primitives/relative-time";
import { useOrgSlug } from "@/components/shell/use-org-slug";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useShortcut } from "@/hooks/use-shortcut";
import { ApiError } from "@/lib/api/errors";
import {
  GROUP_LABELS,
  GROUPS,
  inboxHref,
  isApproval,
  KIND_LABELS,
  type InboxGroup,
  type InboxItem,
  type NotificationKind,
} from "@/lib/api/inbox";
import { shortcut } from "@/lib/shortcut-registry";

import { useInbox, useInboxRead } from "./use-inbox";

const SHOW = ["unread", "all"] as const;

const ICONS: Record<NotificationKind, LucideIcon> = {
  approval_needed: ShieldQuestion,
  pr_opened: GitPullRequest,
  index_done: CircleCheck,
  fix_failed: CircleAlert,
  index_failed: CircleAlert,
  cost_cap: Gauge,
};

const ROW = "[data-inbox-row]";

export function InboxPage() {
  const org = useOrgSlug();
  const router = useRouter();
  const [show, setShow] = useQueryState(
    "show",
    parseAsStringLiteral(SHOW).withDefault("unread").withOptions({ history: "replace" }),
  );
  const [group, setGroup] = useQueryState(
    "group",
    parseAsStringLiteral(GROUPS).withOptions({ history: "replace" }),
  );
  const inbox = useInbox(org, show === "unread", group);
  const { read, busy } = useInboxRead(org);
  const listRef = useRef<HTMLUListElement>(null);

  const pages = inbox.data?.pages ?? [];
  const items = pages.flatMap((page) => page.items);
  const unread = pages[0]?.unread ?? 0;

  const rows = () => [...(listRef.current?.querySelectorAll<HTMLElement>(ROW) ?? [])];
  // The item focus is in: its row's link, or its Done button beside it.
  const holds = (row: HTMLElement) => row.closest("li")?.contains(document.activeElement) ?? false;
  const focused = (): { row: HTMLElement; item: InboxItem } | null => {
    const row = rows().find(holds);
    const item = row ? items.find((candidate) => candidate.id === row.dataset.id) : undefined;
    return row && item ? { row, item } : null;
  };
  const move = (step: 1 | -1) => {
    const all = rows();
    if (all.length === 0) return;
    const at = all.findIndex(holds);
    const next = at === -1 ? (step === 1 ? 0 : all.length - 1) : at + step;
    all[Math.max(0, Math.min(all.length - 1, next))]?.focus();
  };
  const done = async (item: InboxItem, row?: HTMLElement) => {
    if (row) {
      // On to the next one first: in the unread view, this one is about to go.
      const all = rows();
      const at = all.indexOf(row);
      (all[at + 1] ?? all[at - 1])?.focus();
    }
    if (item.read) return;
    try {
      await read([item.id]);
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : "It couldn't be marked done.");
    }
  };
  const open = (item: InboxItem) => {
    const href = inboxHref(item);
    if (!href) return;
    if (!item.read) void read([item.id]).catch(() => undefined);
    router.push(href as never);
  };

  useShortcut("list-next", () => {
    move(1);
  });
  useShortcut("list-prev", () => {
    move(-1);
  });
  useShortcut("inbox-done", () => {
    const at = focused();
    if (at) void done(at.item, at.row);
  });
  useShortcut("inbox-approve", () => {
    const at = focused();
    if (at && isApproval(at.item)) open(at.item);
  });

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6" data-testid="inbox">
      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">Inbox</h1>
        <span className="text-sm text-fg-muted" data-testid="inbox-unread">
          {unread === 0 ? "Nothing unread" : `${String(unread)} unread`}
        </span>
        <Button
          variant="outline"
          size="sm"
          className="ml-auto"
          disabled={busy || unread === 0}
          onClick={() => {
            // Of the kinds shown: what isn't on the page stays as it was.
            void read(undefined, group).catch((caught: unknown) => {
              toast.error(caught instanceof ApiError ? caught.message : "Couldn't mark them read.");
            });
          }}
        >
          <CheckCheck aria-hidden />
          Mark all read
        </Button>
      </header>
      <div className="flex flex-wrap items-center gap-3">
        <ToggleGroup
          aria-label="Show"
          value={[show]}
          onValueChange={(value: string[]) => {
            const next = value[0];
            if (next === "unread" || next === "all") void setShow(next);
          }}
        >
          <ToggleGroupItem value="unread" size="sm">
            Unread
          </ToggleGroupItem>
          <ToggleGroupItem value="all" size="sm">
            All
          </ToggleGroupItem>
        </ToggleGroup>
        <ToggleGroup
          aria-label="Kind"
          value={[group ?? "any"]}
          onValueChange={(value: string[]) => {
            const next = value[0];
            void setGroup(GROUPS.find((g) => g === next) ?? null);
          }}
        >
          <ToggleGroupItem value="any" size="sm">
            Everything
          </ToggleGroupItem>
          {GROUPS.map((value) => (
            <ToggleGroupItem key={value} value={value} size="sm">
              {GROUP_LABELS[value]}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        <p className="ml-auto hidden items-center gap-1.5 text-xs text-fg-muted sm:flex">
          <KeyHint shortcut={shortcut("list-next").keys} />
          <KeyHint shortcut={shortcut("list-prev").keys} /> move ·
          <KeyHint shortcut={shortcut("inbox-approve").keys} /> approve ·
          <KeyHint shortcut={shortcut("inbox-done").keys} /> done
        </p>
      </div>
      {inbox.isPending ? (
        <div role="status" aria-label="Loading the inbox" className="flex flex-col gap-2">
          <Skeleton className="h-14" />
          <Skeleton className="h-14" />
        </div>
      ) : inbox.isError ? (
        <ErrorState title="Couldn't load the inbox" onRetry={() => void inbox.refetch()} />
      ) : items.length === 0 ? (
        <Empty show={show} group={group} />
      ) : (
        <>
          <ul ref={listRef} className="flex flex-col gap-1" aria-label="Inbox items">
            {items.map((item) => (
              <Row
                key={item.id}
                item={item}
                onOpen={() => {
                  if (!item.read) void read([item.id]).catch(() => undefined);
                }}
                onDone={(row) => void done(item, row)}
              />
            ))}
          </ul>
          {inbox.hasNextPage && (
            <Button
              variant="ghost"
              size="sm"
              className="self-center"
              disabled={inbox.isFetchingNextPage}
              onClick={() => void inbox.fetchNextPage()}
            >
              Load older
            </Button>
          )}
        </>
      )}
    </div>
  );
}

function Empty({ show, group }: { show: (typeof SHOW)[number]; group: InboxGroup | null }) {
  const what = group ? GROUP_LABELS[group].toLowerCase() : null;
  return (
    <EmptyState
      icon={<Inbox aria-hidden />}
      title={show === "unread" ? "You're all caught up" : "Nothing here yet"}
      description={
        show === "unread"
          ? `Nothing unread${what ? ` in ${what}` : ""}. Plans waiting for your approval, pull requests opened and runs that stopped show up here.`
          : `Nothing${what ? ` in ${what}` : ""} yet. You'll be told here when a plan waits for your approval, a pull request opens or a run stops.`
      }
    />
  );
}

function Row({
  item,
  onOpen,
  onDone,
}: {
  item: InboxItem;
  onOpen: () => void;
  onDone: (row: HTMLElement) => void;
}) {
  const Icon = ICONS[item.kind];
  const href = inboxHref(item);
  const rowRef = useRef<HTMLElement | null>(null);
  const focus =
    "rounded-sm focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none";
  const title = (
    <span className={cn("text-sm", item.read ? "text-fg-muted" : "font-medium")}>{item.title}</span>
  );
  return (
    <li
      className={cn(
        "relative flex items-start gap-3 rounded-md border border-line px-3 py-2.5",
        "focus-within:border-line-strong hover:bg-surface-2",
        !item.read && "bg-surface-1",
      )}
      data-testid="inbox-item"
      data-kind={item.kind}
      data-read={item.read}
    >
      <Icon
        aria-hidden
        className={cn(
          "mt-0.5 size-4 shrink-0",
          item.kind === "approval_needed" ? "text-signal" : "text-fg-muted",
          (item.kind === "fix_failed" || item.kind === "index_failed") && "text-danger",
        )}
      />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="sr-only">{KIND_LABELS[item.kind]}: </span>
        {href ? (
          <Link
            href={href as never}
            ref={(node) => {
              rowRef.current = node;
            }}
            data-inbox-row
            data-id={item.id}
            onClick={onOpen}
            className={cn(focus, "after:absolute after:inset-0 after:content-['']")}
          >
            {title}
          </Link>
        ) : (
          // Nowhere to go: opening it is reading it.
          <button
            type="button"
            ref={(node) => {
              rowRef.current = node;
            }}
            data-inbox-row
            data-id={item.id}
            onClick={onOpen}
            className={cn(focus, "text-left")}
          >
            {title}
          </button>
        )}
        {item.body && <p className="text-xs text-fg-muted">{item.body}</p>}
        <div className="flex items-center gap-2 text-xs text-fg-faint">
          {item.risk && <RiskBadge risk={item.risk} />}
          <RelativeTime date={item.created_at} />
        </div>
      </div>
      {!item.read && (
        <>
          <span aria-hidden className="mt-1.5 size-2 shrink-0 rounded-full bg-signal" />
          <span className="sr-only">Unread</span>
          <Button
            variant="ghost"
            size="sm"
            className="relative z-10"
            onClick={() => {
              if (rowRef.current) onDone(rowRef.current);
            }}
          >
            Done<span className="sr-only">: {item.title}</span>
          </Button>
        </>
      )}
    </li>
  );
}
