const MENTION = /(?:^|\s)@([\w./-]*)$/;

/** The `@…` being typed just before the caret, if any: where it starts and what's typed. */
export function mentionAt(text: string, caret: number): { start: number; query: string } | null {
  const found = MENTION.exec(text.slice(0, caret));
  if (!found) return null;
  const query = found[1] ?? "";
  return { start: caret - query.length - 1, query };
}
