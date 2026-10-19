import { setupServer } from "msw/node";

import { handlers } from "./handlers";

/** For Vitest: `server.listen()` in beforeAll, `server.resetHandlers()` + `resetMockDb()` after each. */
export const server = setupServer(...handlers);
