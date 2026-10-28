import { browseHandlers } from "./browse";
import { constellationHandlers } from "./constellation";
import { graphHandlers } from "./graph";
import { invitationHandlers } from "./invitations";
import { meHandlers } from "./me";
import { memberHandlers } from "./members";
import { orgHandlers } from "./orgs";
import { repoHandlers } from "./repos";
import { searchHandlers } from "./search";
import { symbolHandlers } from "./symbols";
import { traceHandlers } from "./trace";

export { apiError } from "./errors";

/** Order matters: specific paths (slug-available) are listed before parameterised ones. */
export const handlers = [
  ...meHandlers,
  ...constellationHandlers,
  ...browseHandlers,
  ...symbolHandlers,
  ...graphHandlers,
  ...traceHandlers,
  ...searchHandlers,
  ...repoHandlers,
  ...orgHandlers,
  ...memberHandlers,
  ...invitationHandlers,
];
