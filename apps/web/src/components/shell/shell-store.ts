/**
 * Shell layout state. Sidebar width/collapse and the rail toggle persist in localStorage and are
 * mirrored onto <html> (a CSS variable and data attributes), which an inline script restores
 * before first paint, so the layout never shifts on load.
 */
import type { ReactNode } from "react";
import { create } from "zustand";

export const SIDEBAR_MIN = 200;
export const SIDEBAR_MAX = 360;
export const SIDEBAR_DEFAULT = 240;
export const SHELL_STORAGE_KEY = "sensei.shell.v1";

interface PersistedShell {
  sidebarWidth: number;
  sidebarCollapsed: boolean;
  railOpen: boolean;
}

interface ShellState extends PersistedShell {
  commandOpen: boolean;
  shortcutsOpen: boolean;
  mobileNavOpen: boolean;
  railContent: ReactNode;
  railTitle: string | null;
  setSidebarWidth: (width: number) => void;
  toggleSidebar: () => void;
  toggleRail: () => void;
  setCommandOpen: (open: boolean) => void;
  setShortcutsOpen: (open: boolean) => void;
  setMobileNavOpen: (open: boolean) => void;
  setRail: (title: string | null, content: ReactNode) => void;
  /** Load the persisted layout after mount (the server render always uses the defaults). */
  hydrate: () => void;
}

export function clampSidebarWidth(width: number): number {
  if (!Number.isFinite(width)) return SIDEBAR_DEFAULT; // e.g. a corrupted stored value
  return Math.round(Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, width)));
}

const DEFAULTS: PersistedShell = {
  sidebarWidth: SIDEBAR_DEFAULT,
  sidebarCollapsed: false,
  railOpen: false,
};

export function readPersisted(): PersistedShell {
  const fallback = DEFAULTS;
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.localStorage.getItem(SHELL_STORAGE_KEY);
    const parsed = (raw ? JSON.parse(raw) : {}) as Record<string, unknown>;
    return {
      sidebarWidth: clampSidebarWidth(Number(parsed.sidebarWidth ?? SIDEBAR_DEFAULT)),
      sidebarCollapsed: parsed.sidebarCollapsed === true,
      railOpen: parsed.railOpen === true,
    };
  } catch {
    return fallback;
  }
}

/** Write the persisted part to storage and to <html>, where CSS reads it. */
export function applyShellLayout({ sidebarWidth, sidebarCollapsed, railOpen }: PersistedShell) {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.style.setProperty("--sidebar-width", `${sidebarWidth}px`);
  root.dataset.sidebar = sidebarCollapsed ? "collapsed" : "expanded";
  root.dataset.rail = railOpen ? "open" : "closed";
  try {
    window.localStorage.setItem(
      SHELL_STORAGE_KEY,
      JSON.stringify({ sidebarWidth, sidebarCollapsed, railOpen }),
    );
  } catch {
    // Storage can be unavailable (private mode, quota); the layout still works for this visit.
  }
}

export const useShellStore = create<ShellState>()((set, get) => {
  const persist = () => {
    const { sidebarWidth, sidebarCollapsed, railOpen } = get();
    applyShellLayout({ sidebarWidth, sidebarCollapsed, railOpen });
  };
  return {
    ...DEFAULTS,
    commandOpen: false,
    shortcutsOpen: false,
    mobileNavOpen: false,
    railContent: null,
    railTitle: null,
    setSidebarWidth: (width) => {
      set({ sidebarWidth: clampSidebarWidth(width) });
      persist();
    },
    toggleSidebar: () => {
      set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed }));
      persist();
    },
    toggleRail: () => {
      set((state) => ({ railOpen: !state.railOpen }));
      persist();
    },
    setCommandOpen: (open) => {
      set({ commandOpen: open });
    },
    setShortcutsOpen: (open) => {
      set({ shortcutsOpen: open });
    },
    setMobileNavOpen: (open) => {
      set({ mobileNavOpen: open });
    },
    setRail: (title, content) => {
      set({ railTitle: title, railContent: content });
    },
    hydrate: () => {
      set(readPersisted());
    },
  };
});

/**
 * Runs inline in <head> before first paint (keep it tiny and dependency-free). Mirrors
 * `applyShellLayout`; a test keeps the two in step.
 */
export const SHELL_BOOT_SCRIPT = `(function(){try{var s=JSON.parse(localStorage.getItem(${JSON.stringify(
  SHELL_STORAGE_KEY,
)})||"{}"),r=document.documentElement,n=Number(s.sidebarWidth||${SIDEBAR_DEFAULT}),w=isFinite(n)?Math.round(Math.min(${SIDEBAR_MAX},Math.max(${SIDEBAR_MIN},n))):${SIDEBAR_DEFAULT};r.style.setProperty("--sidebar-width",w+"px");r.dataset.sidebar=s.sidebarCollapsed===true?"collapsed":"expanded";r.dataset.rail=s.railOpen===true?"open":"closed"}catch(e){}})();`;
