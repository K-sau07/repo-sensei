import { describe, expect, it } from "vitest";

import { AUTHOR, jsonLdScript, siteJsonLd } from "./author";

describe("siteJsonLd", () => {
  it("names the app and its author, absolute from the origin", () => {
    const graph = siteJsonLd("https://reposensei.com/")["@graph"];
    const app = graph.find((node) => node["@type"] === "SoftwareApplication");
    const person = graph.find((node) => node["@type"] === "Person");
    expect(app).toMatchObject({
      name: "repo-sensei",
      url: "https://reposensei.com/",
      author: { "@id": "https://reposensei.com/#author" },
    });
    expect(person).toMatchObject({
      "@id": "https://reposensei.com/#author",
      name: "Saurabh Kashyap",
      url: "https://reposensei.com/about",
      alumniOf: { name: "Northeastern University" },
      sameAs: [AUTHOR.linkedin, AUTHOR.github],
    });
  });
});

describe("jsonLdScript", () => {
  it("can't close the script tag it sits in", () => {
    const text = jsonLdScript({ note: "</script><script>alert(1)</script>" });
    expect(text).not.toContain("<");
    expect(JSON.parse(text)).toEqual({ note: "</script><script>alert(1)</script>" });
  });
});
