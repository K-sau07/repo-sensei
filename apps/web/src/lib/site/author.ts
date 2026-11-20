/** Who built repo-sensei, for the footer, the About page and the structured data search engines
 * read (schema.org JSON-LD in the root layout). */

export const AUTHOR = {
  name: "Saurabh Kashyap",
  linkedin: "https://www.linkedin.com/in/saurabh-kashyap-b5a4ab22a/",
  github: "https://github.com/K-sau07",
  school: "Northeastern University",
} as const;

export const REPO_URL = "https://github.com/K-sau07/repo-sensei";

const DESCRIPTION =
  "Ask a codebase anything and get answers that cite the lines they stand on. Trace features across layers and draw diagrams checked against the code.";

/** The site, the app and its author as one schema.org graph, absolute from ``origin``. */
export function siteJsonLd(origin: string) {
  const base = origin.replace(/\/+$/, "");
  const author = `${base}/#author`;
  return {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "WebSite",
        "@id": `${base}/#website`,
        url: `${base}/`,
        name: "repo-sensei",
        publisher: { "@id": author },
      },
      {
        "@type": "SoftwareApplication",
        "@id": `${base}/#app`,
        name: "repo-sensei",
        url: `${base}/`,
        description: DESCRIPTION,
        applicationCategory: "DeveloperApplication",
        operatingSystem: "Web",
        author: { "@id": author },
        sameAs: [REPO_URL],
      },
      {
        "@type": "Person",
        "@id": author,
        name: AUTHOR.name,
        url: `${base}/about`,
        jobTitle: "Software Engineer",
        alumniOf: { "@type": "CollegeOrUniversity", name: AUTHOR.school },
        sameAs: [AUTHOR.linkedin, AUTHOR.github],
      },
    ],
  };
}

/** JSON for an inline ``<script type="application/ld+json">``: a ``<`` in it can't end the tag. */
export function jsonLdScript(data: unknown): string {
  return JSON.stringify(data).replace(/</g, "\\u003c");
}

/** The site's social card (app/opengraph-image.tsx), for pages that set their own Open Graph
 * fields: a page's ``openGraph`` replaces the inherited one, image included. */
export const SITE_CARD = {
  url: "/opengraph-image",
  width: 1200,
  height: 630,
  alt: "repo-sensei: understand any codebase, with answers you can verify",
} as const;
