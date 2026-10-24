"use client";

import { cn } from "cn";
import { Crosshair, Info, ListFilter, Search, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useState, type KeyboardEvent, type RefObject } from "react";

import { KeyHint } from "@/components/primitives/key-hint";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  LAYERS,
  SEARCH_QUERY_MAX,
  type ExactQuery,
  type Interpretation,
  type Layer,
} from "@/lib/api/search";
import { languageName } from "@/lib/code/format";
import { filterCount, searchPath, type SearchState } from "@/lib/search/url-state";
import { shortcut } from "@/lib/shortcut-registry";

import { LANES, QUERY_CLASSES } from "./meta";

/** Typing settles this long before it searches. */
export const TYPING_MS = 200;

type SetState = (next: Partial<SearchState>, options?: { history?: "push" | "replace" }) => unknown;

/**
 * The search field. What's typed searches once typing pauses; a pause makes one history entry
 * for the whole run of typing (so Back returns to the search before it, not to each pause), and
 * ↵ searches at once. A query changed from elsewhere (Back, a pivot) replaces what's typed.
 */
export function SearchInput({
  value,
  onSearch,
  inputRef,
  onDown,
}: {
  value: string;
  onSearch: (q: string, history: "push" | "replace") => void;
  inputRef: RefObject<HTMLInputElement | null>;
  onDown: () => void;
}) {
  const [draft, setDraft] = useState(value);
  // The URL's query when last seen, and the query this field last sent.
  const [seen, setSeen] = useState(value);
  const [sent, setSent] = useState(value);
  // In a run of typing: its pauses replace one history entry.
  const [typing, setTyping] = useState(false);

  // The URL moved on without us (Back, Forward, a pivot): show its query.
  const external = value !== seen && value !== sent;
  if (value !== seen) {
    setSeen(value);
    if (external) {
      setDraft(value);
      setSent(value);
      setTyping(false);
    }
  }

  const commit = (q: string, history: "push" | "replace") => {
    if (q === sent) return;
    setSent(q);
    onSearch(q, history);
  };

  useEffect(() => {
    if (draft === sent) return;
    const timer = setTimeout(() => {
      commit(draft, typing ? "replace" : "push");
      setTyping(true);
    }, TYPING_MS);
    return () => {
      clearTimeout(timer);
    };
  });

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commit(draft, typing ? "replace" : "push");
      setTyping(false);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      onDown();
    }
  };

  return (
    <div className="relative min-w-0 flex-1">
      <Search
        aria-hidden
        className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-fg-muted"
      />
      <Input
        ref={inputRef}
        type="search"
        aria-label="Search the code"
        placeholder="A name, a phrase, “exact text” or a regex…"
        maxLength={SEARCH_QUERY_MAX}
        autoComplete="off"
        spellCheck={false}
        value={draft}
        onChange={(event) => {
          setDraft(event.target.value);
        }}
        onKeyDown={onKeyDown}
        onBlur={() => {
          setTyping(false);
        }}
        className="peer h-9 pr-10 pl-8 text-sm"
      />
      <KeyHint
        shortcut={shortcut("focus-search").keys}
        className="pointer-events-none absolute top-1/2 right-2.5 -translate-y-1/2 peer-focus:hidden"
      />
    </div>
  );
}

/** How the query was read, as a chip; its popover explains, with the rewrites and literals. */
export function QueryClassChip({
  interpretation,
  exact,
}: {
  interpretation: Interpretation | null;
  /** An exact search runs the query as typed (`mode`). */
  exact: "keyword" | "regex" | null;
}) {
  if (!interpretation && !exact) return null;
  const meta = exact
    ? {
        label: exact === "regex" ? "regex · exact" : "keyword · exact",
        explain:
          exact === "regex"
            ? "Run as a regular expression over every file, line by line, with nothing else added."
            : "Matched by its words only: no related words, nothing by meaning.",
      }
    : QUERY_CLASSES[interpretation?.class ?? "conceptual"];
  const rewrites = exact ? [] : (interpretation?.rewrites ?? []);
  const literals = exact ? [] : (interpretation?.literals ?? []);
  const pathFilter = exact ? null : (interpretation?.path_filter ?? null);
  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button
            variant="outline"
            size="sm"
            className="h-7 shrink-0 gap-1.5"
            aria-label={`Read as ${meta.label}: how the query was read`}
          />
        }
      >
        <Info aria-hidden className="text-signal" />
        <span>{meta.label}</span>
      </PopoverTrigger>
      <PopoverContent
        aria-label="How the query was read"
        align="start"
        className="w-80 gap-3 text-sm"
      >
        <p>{meta.explain}</p>
        {rewrites.length > 0 && (
          <div className="flex flex-col gap-1">
            <h3 className="text-xs font-medium text-fg-muted">Also searched for</h3>
            <ul aria-label="Rewrites" className="flex flex-wrap gap-1">
              {rewrites.map((rewrite) => (
                <li
                  key={rewrite}
                  className="rounded-sm bg-surface-3 px-1.5 py-0.5 font-mono text-xs"
                >
                  {rewrite}
                </li>
              ))}
            </ul>
          </div>
        )}
        {literals.length > 0 && (
          <div className="flex flex-col gap-1">
            <h3 className="text-xs font-medium text-fg-muted">Kept exactly as typed</h3>
            <ul aria-label="Literals" className="flex flex-wrap gap-1">
              {literals.map((literal) => (
                <li
                  key={literal}
                  className="rounded-sm bg-surface-3 px-1.5 py-0.5 font-mono text-xs"
                >
                  {literal}
                </li>
              ))}
            </ul>
          </div>
        )}
        {pathFilter && (
          <p className="text-xs text-fg-muted">
            Only under <code className="font-mono text-fg">{pathFilter}</code>
          </p>
        )}
      </PopoverContent>
    </Popover>
  );
}

/** Language, path prefix, layer and ref, all in the URL. */
export function FiltersPopover({
  state,
  setState,
  languages,
}: {
  state: SearchState;
  setState: SetState;
  /** Languages to offer: those in the results, and any already picked. */
  languages: readonly string[];
}) {
  const count = filterCount(state);
  const offered = [...new Set([...languages, ...state.lang])].sort();
  const toggle = <T extends string>(list: readonly T[], value: T, on: boolean): T[] =>
    on ? [...list, value] : list.filter((item) => item !== value);
  return (
    <Popover>
      <PopoverTrigger render={<Button variant="outline" size="sm" className="h-9 shrink-0" />}>
        <ListFilter aria-hidden />
        Filters{" "}
        {count > 0 && (
          <span className="rounded-sm bg-surface-3 px-1 text-xs tabular-nums">
            {count}
            <span className="sr-only"> set</span>
          </span>
        )}
      </PopoverTrigger>
      <PopoverContent aria-label="Search filters" align="end" className="w-80 gap-4">
        <fieldset className="flex flex-col gap-1.5">
          <legend className="mb-1 text-xs font-medium text-fg-muted">Layer</legend>
          {LAYERS.map((layer) => (
            <Label key={layer} className="flex items-center gap-2 text-sm font-normal">
              <Checkbox
                checked={state.layer.includes(layer)}
                onCheckedChange={(on) => {
                  void setState({ layer: toggle<Layer>(state.layer, layer, on) });
                }}
              />
              <span aria-hidden className={LANES[layer].text}>
                {LANES[layer].glyph}
              </span>
              {LANES[layer].label}
            </Label>
          ))}
        </fieldset>
        {offered.length > 0 && (
          <fieldset className="flex flex-col gap-1.5">
            <legend className="mb-1 text-xs font-medium text-fg-muted">Language</legend>
            {offered.map((language) => (
              <Label key={language} className="flex items-center gap-2 text-sm font-normal">
                <Checkbox
                  checked={state.lang.includes(language)}
                  onCheckedChange={(on) => {
                    void setState({ lang: toggle(state.lang, language, on) });
                  }}
                />
                {languageName(language) ?? language}
              </Label>
            ))}
          </fieldset>
        )}
        <TextFilter
          label="Path starts with"
          placeholder="backend/app/"
          value={state.path}
          onApply={(path) => void setState({ path })}
        />
        <TextFilter
          label="Branch or commit"
          placeholder="The last index"
          value={state.ref ?? ""}
          onApply={(ref) => void setState({ ref: ref || null })}
        />
        {count > 0 && (
          <Button
            variant="ghost"
            size="sm"
            className="self-start"
            onClick={() => void setState({ layer: [], lang: [], path: "", ref: null })}
          >
            <X aria-hidden data-icon="inline-start" />
            Clear filters
          </Button>
        )}
      </PopoverContent>
    </Popover>
  );
}

function TextFilter({
  label,
  placeholder,
  value,
  onApply,
}: {
  label: string;
  placeholder: string;
  value: string;
  onApply: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  const [seen, setSeen] = useState(value);
  if (value !== seen) {
    setSeen(value);
    setDraft(value);
  }
  const apply = () => {
    if (draft.trim() !== value) onApply(draft.trim());
  };
  return (
    <Label className="flex flex-col items-stretch gap-1.5 text-xs font-medium text-fg-muted">
      {label}
      <Input
        value={draft}
        placeholder={placeholder}
        spellCheck={false}
        className="font-mono text-sm font-normal text-fg"
        onChange={(event) => {
          setDraft(event.target.value);
        }}
        onBlur={apply}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            apply();
          }
        }}
      />
    </Label>
  );
}

const EXACT_LABELS: Record<ExactQuery["kind"], string> = {
  literal: "literal",
  regex: "regex",
  symbol: "symbol",
  path: "path",
};

/** The state an exact query runs as: literal, symbol and path queries as a keyword search,
 * regex as an exact search over the files. */
export function exactState(state: SearchState, query: ExactQuery): Partial<SearchState> {
  if (query.kind === "regex") return { ...state, q: query.query, mode: "regex" };
  if (query.kind === "path") return { ...state, path: query.query, mode: "keyword" };
  return { ...state, q: query.query, mode: "keyword" };
}

/** "Pivot to exact": the deterministic queries behind the results, each runnable. */
export function PivotToExact({
  org,
  repo,
  state,
  queries,
}: {
  org: string;
  repo: string;
  state: SearchState;
  queries: readonly ExactQuery[];
}) {
  // A symbol and a literal with the same text run the same keyword search: list it once.
  const seen = new Set<string>();
  const runnable = queries.filter((query) => {
    const href = searchPath(org, repo, exactState(state, query));
    if (seen.has(href)) return false;
    seen.add(href);
    return true;
  });
  if (runnable.length === 0 && !state.mode) return null;
  return (
    <nav aria-label="Pivot to exact" className="flex flex-wrap items-center gap-1.5 text-xs">
      <span className="inline-flex items-center gap-1 text-fg-muted">
        <Crosshair aria-hidden className="size-3.5" />
        Pivot to exact
      </span>
      <ul className="flex flex-wrap gap-1.5">
        {runnable.map((query) => (
          <li key={`${query.kind}:${query.query}`}>
            <Link
              href={searchPath(org, repo, exactState(state, query))}
              className="inline-flex h-6 max-w-80 items-center gap-1.5 rounded-sm border border-line bg-surface-1 px-1.5 hover:border-line-strong hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
            >
              <span className="text-fg-muted">{EXACT_LABELS[query.kind]}</span>{" "}
              <code className="truncate font-mono text-fg">{query.query}</code>
            </Link>
          </li>
        ))}
        {state.mode && (
          <li>
            <Link
              href={searchPath(org, repo, { ...state, mode: null })}
              className={cn(
                "inline-flex h-6 items-center gap-1 rounded-sm px-1.5 text-fg-muted underline underline-offset-4 hover:text-fg",
              )}
            >
              Back to smart search
            </Link>
          </li>
        )}
      </ul>
    </nav>
  );
}
