import { setupWorker } from "msw/browser";

import { handlers } from "./handlers";

/** Used when NEXT_PUBLIC_MOCKS=1 (UI work without the API) and by Storybook. */
export const worker = setupWorker(...handlers);
