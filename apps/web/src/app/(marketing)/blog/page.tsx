import type { Metadata } from "next";
import Link from "next/link";

import { POSTS, postPath } from "@/content/blog-index";

const DESCRIPTION = "How repo-sensei works, and what we learn building it.";

export const metadata: Metadata = {
  title: "Blog",
  description: DESCRIPTION,
  alternates: { canonical: "/blog" },
};

const DAY = new Intl.DateTimeFormat("en", { dateStyle: "long", timeZone: "UTC" });

export default function BlogPage() {
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-10 px-6 py-12 sm:py-16">
      <header className="flex flex-col gap-2">
        <h1 className="text-4xl font-semibold tracking-[-0.02em]">Blog</h1>
        <p className="text-lg text-fg-muted">{DESCRIPTION}</p>
      </header>
      <ul className="flex flex-col gap-6">
        {POSTS.map((post) => (
          <li key={post.slug}>
            <Link href={postPath(post.slug)} className="group flex flex-col gap-1">
              <time dateTime={post.date} className="text-sm text-fg-faint">
                {DAY.format(new Date(post.date))}
              </time>
              <h2 className="text-xl font-semibold group-hover:underline">{post.title}</h2>
              <p className="text-fg-muted">{post.description}</p>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
