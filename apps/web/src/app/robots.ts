import type { MetadataRoute } from "next";

/** Crawlers may read the public pages; shared answers, invitations and personal pages stay out of
 * search results (the rest needs a sign-in anyway). */
export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      userAgent: "*",
      allow: "/",
      disallow: ["/s/", "/invite/", "/settings/", "/onboarding", "/logout"],
    },
  };
}
