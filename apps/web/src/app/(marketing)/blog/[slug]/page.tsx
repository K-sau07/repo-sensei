import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { LongPage } from "@/components/marketing/long-page";
import { POSTS, postPath } from "@/content/blog-index";

type Props = PageProps<"/blog/[slug]">;

// Each post is known at build, and one that isn't a post answers 404.
export const instant = false;

export function generateStaticParams() {
  return POSTS.map((post) => ({ slug: post.slug }));
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const post = POSTS.find((p) => p.slug === slug);
  if (!post) return { title: "Not found", robots: { index: false } };
  return {
    title: post.title,
    description: post.description,
    alternates: { canonical: postPath(post.slug) },
    openGraph: { title: post.title, description: post.description, type: "article" },
  };
}

export default async function PostPage({ params }: Props) {
  const { slug } = await params;
  const post = POSTS.find((p) => p.slug === slug);
  if (!post) notFound();
  const { default: Content } = await post.load();
  return (
    <LongPage title={post.title} lead={post.description} updated={post.date} dated="Published">
      <Content />
    </LongPage>
  );
}
