"use client";

/**
 * The question box (4.10): ⌘↵ (Ctrl↵) sends, Stop ends an answer being written, Fast or Deep
 * (the deep model, for questions across the repository). What is typed is kept as a draft per
 * conversation (it survives a reload and a sign-in again) until it is sent. Keyed by its draft
 * where it is used, so another conversation's box starts from that one's draft.
 *
 * A question can be scoped (4.10.7): to a file or a folder and to layers, picked from Scope and
 * shown as chips that stay for the questions after it until removed. `@` mentions a file or a
 * symbol: typing after it lists matches (the files and symbols searches, as the palette's), and
 * picking one writes its name in backticks.
 */
import { ArrowUp, Crosshair, FileCode2, Square, X } from "lucide-react";
import { useId, useRef, useState } from "react";

import { useServerResults } from "@/components/palette/use-server-results";
import { useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  keepDraft,
  LAYER_LABELS,
  pathLabel,
  readDraft,
  SCOPE_LAYERS,
  scopeOrNull,
  type AskMode,
  type ScopeData,
  type ScopeLayer,
} from "@/lib/api/ask";

const MENTION = /(?:^|\s)@([\w./-]*)$/;
const MAX_MENTIONS = 8;

interface Mention {
  label: string;
  detail: string;
  insert: string;
}

/** The `@…` being typed just before the caret, if any: where it starts and what's typed. */
export function mentionAt(text: string, caret: number): { start: number; query: string } | null {
  const found = MENTION.exec(text.slice(0, caret));
  if (!found) return null;
  const query = found[1] ?? "";
  return { start: caret - query.length - 1, query };
}

function useMentions(query: string | null): Mention[] {
  const { org, repo } = useRepoSlug();
  const { files, symbols } = useServerResults({ org, repo, ref: null }, query ?? "");
  if (query === null) return [];
  return [
    ...symbols.hits.map((hit) => ({
      label: hit.name,
      detail: `${hit.kind} · ${hit.path}`,
      insert: `\`${hit.name}\``,
    })),
    ...files.hits.map((hit) => ({ label: hit.path, detail: "file", insert: `\`${hit.path}\`` })),
  ].slice(0, MAX_MENTIONS);
}

function ScopePicker({ scope, onScope }: { scope: ScopeData; onScope: (s: ScopeData) => void }) {
  const { org, repo } = useRepoSlug();
  const [typed, setTyped] = useState("");
  const { files } = useServerResults({ org, repo, ref: null }, typed);
  const folder = typed.trim().replace(/^\/+/, "");
  const layers = scope.layers ?? [];
  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button type="button" size="sm" variant="ghost" aria-label="Scope the question">
            <Crosshair aria-hidden className="size-3.5" /> Scope
          </Button>
        }
      />
      <PopoverContent aria-label="Scope" className="flex w-80 flex-col gap-3">
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium" id="scope-layers">
            Layers
          </span>
          <ToggleGroup
            aria-labelledby="scope-layers"
            multiple
            value={layers}
            onValueChange={(value: string[]) => {
              const picked = SCOPE_LAYERS.filter((layer) => value.includes(layer));
              onScope({ ...scope, layers: picked });
            }}
          >
            {SCOPE_LAYERS.map((layer) => (
              <ToggleGroupItem key={layer} value={layer} size="sm">
                {LAYER_LABELS[layer]}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </div>
        <div className="flex flex-col gap-1.5">
          <label htmlFor="scope-path" className="text-xs font-medium">
            A file or folder
          </label>
          <Input
            id="scope-path"
            placeholder="backend/app/ or items.py"
            value={typed}
            onChange={(event) => {
              setTyped(event.target.value);
            }}
          />
          <ul className="flex max-h-48 flex-col overflow-auto" aria-label="Paths">
            {folder.endsWith("/") ? (
              <li>
                <button
                  type="button"
                  className="w-full truncate rounded-sm px-1.5 py-1 text-left font-mono text-xs hover:bg-surface-2"
                  onClick={() => {
                    onScope({
                      ...scope,
                      path: folder.replace(/\/+$/, ""),
                      startLine: null,
                      endLine: null,
                    });
                    setTyped("");
                  }}
                >
                  Folder {folder}
                </button>
              </li>
            ) : null}
            {files.hits.slice(0, MAX_MENTIONS).map((hit) => (
              <li key={hit.path}>
                <button
                  type="button"
                  className="w-full truncate rounded-sm px-1.5 py-1 text-left font-mono text-xs hover:bg-surface-2"
                  onClick={() => {
                    onScope({ ...scope, path: hit.path, startLine: null, endLine: null });
                    setTyped("");
                  }}
                >
                  {hit.path}
                </button>
              </li>
            ))}
          </ul>
        </div>
      </PopoverContent>
    </Popover>
  );
}

function Chip({
  label,
  title,
  onRemove,
}: {
  label: string;
  title?: string | undefined;
  onRemove: () => void;
}) {
  return (
    <span
      className="flex items-center gap-1 rounded-sm border border-border px-1.5 py-0.5 font-mono text-[11px]"
      title={title}
      data-testid="scope-chip"
    >
      {label}
      <button
        type="button"
        aria-label={`Remove the scope ${label}`}
        className="text-fg-muted hover:text-fg"
        onClick={onRemove}
      >
        <X aria-hidden className="size-3" />
      </button>
    </span>
  );
}

export const NO_SCOPE: ScopeData = { path: null, startLine: null, endLine: null, layers: [] };

export function Composer({
  draftKey,
  busy,
  mode,
  onMode,
  scope,
  onScope,
  onSend,
  onStop,
}: {
  draftKey: string;
  busy: boolean;
  mode: AskMode;
  onMode: (mode: AskMode) => void;
  /** The page's: it stays for the questions after one (the box is keyed by its draft). */
  scope: ScopeData;
  onScope: (scope: ScopeData) => void;
  onSend: (text: string, scope: ScopeData | null) => void;
  onStop: () => void;
}) {
  // Its draft, read once: the page gives each conversation's box a key of its own.
  const [text, setText] = useState(() => readDraft(draftKey));
  const setScope = onScope;
  const [caret, setCaret] = useState(0);
  const [active, setActive] = useState(0);
  const [dismissed, setDismissed] = useState<number | null>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const listId = useId();
  const mention = mentionAt(text, caret);
  const open = mention !== null && dismissed !== mention.start;
  const options = useMentions(open ? mention.query : null);
  const shown = open && options.length > 0;
  const picked = Math.min(active, options.length - 1);

  function change(value: string, at: number) {
    setText(value);
    setCaret(at);
    setActive(0);
    keepDraft(draftKey, value);
  }

  function send() {
    const question = text.trim();
    if (!question || busy) return;
    onSend(question, scopeOrNull(scope));
    change("", 0);
  }

  function pick(option: Mention) {
    if (!mention) return;
    const before = text.slice(0, mention.start);
    const after = text.slice(caret);
    const next = `${before}${option.insert} ${after}`;
    const at = before.length + option.insert.length + 1;
    change(next, at);
    requestAnimationFrame(() => {
      box.current?.setSelectionRange(at, at);
      box.current?.focus();
    });
  }

  const pathChip = pathLabel(scope);
  return (
    <form
      className="bg-surface flex flex-col gap-2 rounded-lg border border-border p-2"
      onSubmit={(event) => {
        event.preventDefault();
        send();
      }}
    >
      {pathChip || scope.layers?.length ? (
        <div className="flex flex-wrap items-center gap-1" aria-label="Scope of the question">
          {pathChip ? (
            <span className="flex items-center gap-1">
              <FileCode2 aria-hidden className="size-3 text-fg-muted" />
              <Chip
                label={pathChip}
                title={scope.path ?? undefined}
                onRemove={() => {
                  setScope({ ...scope, path: null, startLine: null, endLine: null });
                }}
              />
            </span>
          ) : null}
          {(scope.layers ?? []).map((layer: ScopeLayer) => (
            <Chip
              key={layer}
              label={LAYER_LABELS[layer]}
              onRemove={() => {
                setScope({ ...scope, layers: (scope.layers ?? []).filter((l) => l !== layer) });
              }}
            />
          ))}
        </div>
      ) : null}
      <div className="relative">
        <Textarea
          ref={box}
          aria-label="Ask about this repository"
          aria-controls={shown ? listId : undefined}
          aria-activedescendant={shown ? `${listId}-${String(picked)}` : undefined}
          placeholder="Ask how something works, where it lives, what calls it… (@ to mention)"
          value={text}
          rows={3}
          className="resize-none border-0 shadow-none focus-visible:ring-0"
          onChange={(event) => {
            change(event.target.value, event.target.selectionStart);
          }}
          onSelect={(event) => {
            setCaret(event.currentTarget.selectionStart);
          }}
          onKeyDown={(event) => {
            if (shown) {
              if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                const step = event.key === "ArrowDown" ? 1 : -1;
                setActive((picked + step + options.length) % options.length);
                return;
              }
              const option = options[picked];
              if ((event.key === "Enter" || event.key === "Tab") && option && !event.metaKey) {
                event.preventDefault();
                pick(option);
                return;
              }
              if (event.key === "Escape") {
                event.preventDefault();
                setDismissed(mention.start);
                return;
              }
            }
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              send();
            }
          }}
        />
        {shown ? (
          <ul
            id={listId}
            role="listbox"
            aria-label="Mention a file or symbol"
            className="absolute bottom-full left-0 z-10 mb-1 max-h-60 w-full overflow-auto rounded-md border border-border bg-popover p-1 shadow-md"
          >
            {options.map((option, i) => (
              <li
                key={`${option.detail}-${option.label}`}
                id={`${listId}-${String(i)}`}
                role="option"
                aria-selected={i === picked}
                className="flex cursor-pointer items-baseline gap-2 rounded-sm px-2 py-1 text-xs aria-selected:bg-surface-2"
                onMouseDown={(event) => {
                  event.preventDefault(); // keep the caret in the box
                  pick(option);
                }}
              >
                <span className="truncate font-mono">{option.label}</span>
                <span className="ml-auto shrink-0 truncate text-fg-muted">{option.detail}</span>
              </li>
            ))}
          </ul>
        ) : null}
        <p className="sr-only" aria-live="polite">
          {shown ? `${String(options.length)} suggestions` : ""}
        </p>
      </div>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1">
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
          <ScopePicker scope={scope} onScope={setScope} />
        </div>
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
