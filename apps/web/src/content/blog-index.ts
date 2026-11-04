/** The blog's posts (10.5), newest first: metadata here, each post's MDX loaded by its route. */
import type { Route } from "next";
import type { ComponentType } from "react";

export interface Post {
  slug: string;
  title: string;
  date: string; // ISO day
  description: string;
  load: () => Promise<{ default: ComponentType }>;
}

export const POSTS: readonly Post[] = [
  {
    slug: "how-an-answer-is-checked",
    title: "How an answer is checked",
    date: "2026-11-04",
    description:
      "Between your question and the answer you read: evidence first, a citation for every claim, and checks that rewrite what the code doesn't back.",
    load: () => import("./blog/how-an-answer-is-checked.mdx"),
  },
];

export function postPath(slug: string): Route {
  return `/blog/${slug}` as Route;
}
