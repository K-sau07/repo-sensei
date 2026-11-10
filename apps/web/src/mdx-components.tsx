/**
 * How MDX content (src/content: docs, blog, legal) renders: the site's own type, links and code.
 * Required by @next/mdx for the App Router.
 */
import type { MDXComponents } from "mdx/types";
import Link from "next/link";
import type { ComponentProps } from "react";

function Anchor({ href = "", children }: ComponentProps<"a">) {
  if (href.startsWith("/")) {
    // A page of this site: client-side navigation. (Typed routes can't check MDX's strings.)
    return <Link href={href as never}>{children}</Link>;
  }
  return (
    <a href={href} rel="noreferrer noopener">
      {children}
    </a>
  );
}

/** The site's address for ``to`` (privacy@, security@ its domain), from the site's origin. */
export function contactAddress(to: "privacy" | "security"): string {
  const origin = process.env.SENSEI_WEB_ORIGIN ?? "http://localhost:3000";
  return `${to}@${new URL(origin).hostname.replace(/^www\./, "")}`;
}

function Contact({ to }: { to: "privacy" | "security" }) {
  const address = contactAddress(to);
  return <a href={`mailto:${address}`}>{address}</a>;
}

const components: MDXComponents = {
  a: Anchor,
  Contact,
};

export function useMDXComponents(): MDXComponents {
  return components;
}
