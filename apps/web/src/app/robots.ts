import type { MetadataRoute } from "next";

/** Crawlers may read the public pages. Shared answers and invitations aren't disallowed here on
 * purpose: their pages say `noindex` (and their token is in the URL), and a crawler that may not
 * fetch a page never reads that, so it could list the URL bare. Personal pages need a sign-in. */
export default function robots(): MetadataRoute.Robots {
  return {
    rules: { userAgent: "*", allow: "/", disallow: ["/settings/", "/onboarding", "/logout"] },
  };
}
