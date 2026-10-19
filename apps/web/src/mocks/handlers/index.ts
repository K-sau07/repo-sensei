import { invitationHandlers } from "./invitations";
import { meHandlers } from "./me";
import { memberHandlers } from "./members";
import { orgHandlers } from "./orgs";

export { apiError } from "./errors";

/** Order matters: specific paths (slug-available) are listed before parameterised ones. */
export const handlers = [...meHandlers, ...orgHandlers, ...memberHandlers, ...invitationHandlers];
