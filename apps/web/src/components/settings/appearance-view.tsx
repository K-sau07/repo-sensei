"use client";

/**
 * Appearance (10.7.1): the theme, an accent and a contrast level for its palette, the density,
 * and motion and transparency reduced whatever the system says. Each change applies at once and
 * is kept in this browser (lib/appearance.ts); nothing is sent anywhere.
 */
import { useTheme } from "next-themes";
import { useSyncExternalStore } from "react";

import { Button } from "@/components/ui/button";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Switch } from "@/components/ui/switch";
import {
  ACCENT_HUES,
  DEFAULT_APPEARANCE,
  readAppearance,
  saveAppearance,
  savedAppearance,
  serverAppearance,
  subscribeAppearance,
  type Appearance,
  type Density,
} from "@/lib/appearance";
import { generateTheme } from "@/lib/theme/generate";

const THEME_CHOICES = [
  { value: "system", label: "Like the system" },
  { value: "dark", label: "Dark" },
  { value: "light", label: "Light" },
] as const;

const DENSITIES: readonly { value: Density; label: string; help: string }[] = [
  { value: "comfortable", label: "Comfortable", help: "Room around rows and controls." },
  { value: "compact", label: "Compact", help: "More on the screen at once." },
];

const noSubscription = () => () => undefined;
const inTheBrowser = () => true;
const onTheServer = () => false;

export function AppearanceView() {
  const { theme, resolvedTheme, setTheme } = useTheme();
  // The theme is known in the browser only: none is shown checked until then.
  const inBrowser = useSyncExternalStore(noSubscription, inTheBrowser, onTheServer);
  const saved = useSyncExternalStore(subscribeAppearance, savedAppearance, serverAppearance);
  const now = readAppearance(saved);
  const base = inBrowser && resolvedTheme === "light" ? "light" : "dark";
  const change = (changes: Partial<Appearance>) => {
    saveAppearance({ ...now, ...changes });
  };

  return (
    <div className="flex flex-col gap-8">
      <Group title="Theme" id="appearance-theme">
        <RadioGroup
          aria-labelledby="appearance-theme"
          value={inBrowser ? (theme ?? "system") : null}
          onValueChange={(value) => {
            setTheme(String(value));
          }}
          className="flex flex-wrap gap-4"
        >
          {THEME_CHOICES.map((choice) => (
            <label key={choice.value} className="flex items-center gap-2 text-sm">
              <RadioGroupItem value={choice.value} />
              {choice.label}
            </label>
          ))}
        </RadioGroup>
      </Group>

      <Group
        title="Accent"
        id="appearance-accent"
        help="For buttons, links and focus rings. The color for live and verified things stays as it is."
      >
        <RadioGroup
          aria-labelledby="appearance-accent"
          value={now.accentHue === null ? "none" : String(now.accentHue)}
          onValueChange={(value) => {
            change({ accentHue: value === "none" ? null : Number(value) });
          }}
          className="flex flex-wrap gap-3"
        >
          {ACCENT_HUES.map((accent) => {
            const swatch = generateTheme({ base, accentHue: accent.hue }).brand;
            return (
              <label
                key={accent.label}
                className="flex items-center gap-2 rounded-md border border-line px-2.5 py-1.5 text-sm has-[[data-checked]]:border-line-strong"
              >
                <RadioGroupItem value={accent.hue === null ? "none" : String(accent.hue)} />
                <span
                  aria-hidden
                  className="size-3.5 rounded-full border border-line"
                  style={{ backgroundColor: swatch }}
                />
                {accent.label}
              </label>
            );
          })}
        </RadioGroup>
      </Group>

      <Group title="Density" id="appearance-density">
        <RadioGroup
          aria-labelledby="appearance-density"
          value={now.density}
          onValueChange={(value) => {
            change({ density: value === "compact" ? "compact" : "comfortable" });
          }}
          className="flex flex-col gap-2"
        >
          {DENSITIES.map((choice) => (
            <label key={choice.value} className="flex items-start gap-2 text-sm">
              <RadioGroupItem value={choice.value} className="mt-0.5" />
              <span>
                {choice.label}
                <span className="block text-xs text-fg-muted">{choice.help}</span>
              </span>
            </label>
          ))}
        </RadioGroup>
      </Group>

      <Group title="Accessibility" id="appearance-access">
        <Toggle
          id="appearance-contrast"
          label="High contrast"
          help="Text and borders stand further from their backgrounds."
          checked={now.contrast === 1}
          onChange={(on) => {
            change({ contrast: on ? 1 : 0 });
          }}
        />
        <Toggle
          id="appearance-motion"
          label="Reduce motion"
          help="Off: as your system's setting says."
          checked={now.motion === "reduce"}
          onChange={(on) => {
            change({ motion: on ? "reduce" : "system" });
          }}
        />
        <Toggle
          id="appearance-transparency"
          label="Reduce transparency"
          help="Solid backgrounds instead of frosted glass. Off: as your system's setting says."
          checked={now.transparency === "reduce"}
          onChange={(on) => {
            change({ transparency: on ? "reduce" : "system" });
          }}
        />
      </Group>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            saveAppearance(DEFAULT_APPEARANCE);
          }}
        >
          Back to the defaults
        </Button>
        <p className="text-xs text-fg-muted">Kept in this browser only.</p>
      </div>
    </div>
  );
}

function Group({
  title,
  id,
  help,
  children,
}: {
  title: string;
  id: string;
  help?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-3" aria-labelledby={id}>
      <div>
        <h2 id={id} className="font-medium">
          {title}
        </h2>
        {help && <p className="text-sm text-fg-muted">{help}</p>}
      </div>
      {children}
    </section>
  );
}

function Toggle({
  id,
  label,
  help,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  help: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="flex flex-col gap-0.5">
        <label htmlFor={id} className="text-sm">
          {label}
        </label>
        <p id={`${id}-help`} className="text-xs text-fg-muted">
          {help}
        </p>
      </div>
      <Switch
        id={id}
        checked={checked}
        aria-describedby={`${id}-help`}
        onCheckedChange={(value) => {
          onChange(value);
        }}
      />
    </div>
  );
}
