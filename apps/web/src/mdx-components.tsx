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

const components: MDXComponents = {
  a: Anchor,
};

export function useMDXComponents(): MDXComponents {
  return components;
}
