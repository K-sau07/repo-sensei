import { apiKeyHandlers } from "./api-keys";
import { askHandlers } from "./ask";
import { browseHandlers } from "./browse";
import { constellationHandlers } from "./constellation";
import { depsHandlers } from "./deps";
import { diagramHandlers } from "./diagrams";
import { fixHandlers } from "./fixes";
import { galaxyHandlers } from "./galaxy";
import { graphHandlers } from "./graph";
import { impactHandlers } from "./impact";
import { invitationHandlers } from "./invitations";
import { mcpHandlers } from "./mcp";
import { meHandlers } from "./me";
import { memberHandlers } from "./members";
import { oauthHandlers } from "./oauth";
import { orgHandlers } from "./orgs";
import { overviewHandlers } from "./overview";
import { refHandlers } from "./refs";
import { repoHandlers } from "./repos";
import { runsHandlers } from "./runs";
import { searchHandlers } from "./search";
import { symbolHandlers } from "./symbols";
import { traceHandlers } from "./trace";

export { apiError } from "./errors";

/** Order matters: specific paths (slug-available) are listed before parameterised ones. */
export const handlers = [
  ...meHandlers,
  ...oauthHandlers,
  ...constellationHandlers,
  ...browseHandlers,
  ...symbolHandlers,
  ...graphHandlers,
  ...depsHandlers,
  ...impactHandlers,
  ...galaxyHandlers,
  ...traceHandlers,
  ...askHandlers,
  ...refHandlers,
  ...diagramHandlers,
  ...fixHandlers,
  ...runsHandlers,
  ...overviewHandlers,
  ...searchHandlers,
  ...repoHandlers,
  ...orgHandlers,
  ...memberHandlers,
  ...invitationHandlers,
  ...apiKeyHandlers,
  ...mcpHandlers,
];
