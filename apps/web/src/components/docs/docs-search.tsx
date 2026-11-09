"use client";

/**
 * Search the docs (10.5): ⌘K (Ctrl K) on the public pages, or the button; each page by its
 * title, what it covers and the words people look for it by. Signed in, the app's own ⌘K finds
 * the docs too.
 */
import { BookOpen, Search } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { DOC_SECTIONS, DOCS, docPath } from "@/content/docs-index";

export function DocsSearch() {
  const router = useRouter();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey) && !event.altKey) {
        event.preventDefault();
        setOpen((was) => !was);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, []);

  return (
    <>
      <Button
        type="button"
        variant="outline"
        className="w-full justify-start gap-2 text-fg-muted"
        aria-keyshortcuts="Meta+K Control+K"
        onClick={() => {
          setOpen(true);
        }}
      >
        <Search aria-hidden className="size-4" />
        Search the docs
        <kbd aria-hidden className="ml-auto font-mono text-xs text-fg-faint">
          ⌘K
        </kbd>
      </Button>
      <CommandDialog
        open={open}
        onOpenChange={setOpen}
        title="Search the docs"
        description="Find a docs page by what it covers"
      >
        <Command>
          <CommandInput placeholder="Search the docs…" aria-label="Search the docs" />
          <CommandList>
            <CommandEmpty>No page covers that.</CommandEmpty>
            {DOC_SECTIONS.map((section) => (
              <CommandGroup key={section} heading={section}>
                {DOCS.filter((doc) => doc.section === section).map((doc) => (
                  <CommandItem
                    key={doc.slug}
                    value={doc.title}
                    keywords={[doc.description, ...doc.keywords]}
                    onSelect={() => {
                      setOpen(false);
                      router.push(docPath(doc.slug));
                    }}
                  >
                    <BookOpen aria-hidden />
                    <span className="flex flex-col">
                      <span>{doc.title}</span>
                      <span className="text-xs text-fg-muted">{doc.description}</span>
                    </span>
                  </CommandItem>
                ))}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>
      </CommandDialog>
    </>
  );
}
