/** Motion's DOM animation features, in their own chunk: `LazyMotion` loads them after the page
 * has painted (nothing animates before then that needs them). */
export { domAnimation as default } from "motion/react";
