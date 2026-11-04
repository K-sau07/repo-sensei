/** Whether this browser can draw WebGL2, and with a GPU: the WebGL scenes (the constellation, the
 * landing page's sky) draw still or as pictures otherwise. */
import { isSoftwareRenderer } from "@/lib/constellation";

export interface WebGLSupport {
  webgl2: boolean;
  /** Drawn by a software rasterizer, not a GPU. */
  software: boolean;
}
let support: WebGLSupport | null = null;
/** Whether this browser can draw WebGL2, and with a GPU (asked once). */
export function webGLSupport(): WebGLSupport {
  if (support === null) {
    try {
      const probe = document.createElement("canvas").getContext("webgl2");
      // Firefox names the renderer in RENDERER (its debug extension is deprecated); Chrome and
      // Safari answer a generic "WebKit WebGL" there and the real name through the extension.
      let name: unknown = probe?.getParameter(probe.RENDERER);
      if (probe && typeof name === "string" && /^webkit webgl$/i.test(name)) {
        const debug = probe.getExtension("WEBGL_debug_renderer_info");
        if (debug) name = probe.getParameter(debug.UNMASKED_RENDERER_WEBGL);
      }
      support = {
        webgl2: probe !== null,
        software: isSoftwareRenderer(typeof name === "string" ? name : null),
      };
      // Give the probe's context back now: browsers allow only a few at a time.
      probe?.getExtension("WEBGL_lose_context")?.loseContext();
    } catch {
      support = { webgl2: false, software: false };
    }
  }
  return support;
}
