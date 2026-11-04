/** The public gallery's pages, as the browser asks for one (the landing page's input: is this
 * repository published?). The server reads the same mock gallery directly. */
import { http, HttpResponse } from "msw";

import { mockPublicPage } from "@/mocks/explore";

import { apiError } from "./errors";

export const exploreHandlers = [
  http.get<{ owner: string; name: string }>("*/api/v1/public/repos/:owner/:name", ({ params }) => {
    const page = mockPublicPage(params.owner, params.name);
    return page
      ? HttpResponse.json(page)
      : apiError(404, "repo_not_found", "Repository not found.");
  }),
];
