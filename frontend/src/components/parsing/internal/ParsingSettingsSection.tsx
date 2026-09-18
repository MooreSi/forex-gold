import { useEffect, useState } from "react";
import { cn } from "@/lib/cn";
import { PARSING_CATEGORIES } from "../content/settings";

interface ParsingSettingsSectionProps {
  settings: Record<string, unknown>;
  onSave: (key: string, value: number) => Promise<void>;
}

const TONE_CLASS = {
  accent: "text-accent bg-accent/15",
  remote: "text-remote bg-remote/15",
  profit: "text-profit bg-profit/15",
  warning: "text-warning bg-warning/15",
} as const;

function on(settings: Record<string, unknown>, key: string, fallback: boolean): boolean {
  const raw = settings[key];
  if (raw === undefined || raw === null) return fallback;
  return Boolean(Number(raw));
}

/**
 * Every switch that decides whether a Telegram signal is traded.
 *
 * Rendered from `content/settings.ts`, not hand-written, so "is every row on
 * the screen?" is a question a test can answer. The 2026-08-25 version of this
 * section was an empty stub and nothing noticed.
 */
export function ParsingSettingsSection({ settings, onSave }: ParsingSettingsSectionProps) {
  return (
    <div className="space-y-4">
      {PARSING_CATEGORIES.map((category) => (
        <section key={category.badge}>
          <h3 className="mb-2">
            <span
              className={cn(
                "rounded px-1.5 py-0.5 text-[10px] font-bold tracking-wider",
                TONE_CLASS[category.tone],
              )}
            >
              {category.badge}
            </span>
          </h3>
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
            {category.toggles.map((t) => (
              <label
                key={t.key}
                data-testid={`toggle-${t.key}`}
                className="flex h-full flex-col rounded border border-line bg-surface-2 p-2.5"
              >
                <span className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    aria-label={t.label}
                    checked={on(settings, t.key, t.defaultOn)}
                    onChange={(e) => void onSave(t.key, e.target.checked ? 1 : 0)}
                    className="mt-0.5 accent-accent"
                  />
                  <span className="text-xs font-semibold text-ink-1">{t.label}</span>
                </span>
                <span className="mt-1 text-[11px] leading-snug text-ink-3">
                  {t.description}
                </span>
              </label>
            ))}
          </div>
        </section>
      ))}
      <NumericSettings settings={settings} onSave={onSave} />
    </div>
  );
}

function NumericSettings({ settings, onSave }: ParsingSettingsSectionProps) {
  const [window, setWindow] = useState("");
  const [fallback, setFallback] = useState("");

  useEffect(() => {
    setWindow(String(settings["lk_second_message_match_window_sec"] ?? 300));
    setFallback(String(settings["lk_fallback_sl_pips"] ?? 50));
  }, [settings]);

  return (
    <div className="flex flex-wrap items-end gap-4 border-t border-line pt-3">
      <label className="text-xs text-ink-2">
        Second-message match window
        <input
          aria-label="Second-message match window"
          inputMode="numeric"
          value={window}
          onChange={(e) => setWindow(e.target.value)}
          onBlur={() => void onSave("lk_second_message_match_window_sec", Number(window) || 0)}
          className="num ml-2 w-20 rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
        />
        <span className="ml-1 text-[11px] text-ink-3">seconds</span>
      </label>
      <label className="text-xs text-ink-2">
        Fallback SL distance
        <input
          aria-label="Fallback SL distance"
          inputMode="decimal"
          value={fallback}
          onChange={(e) => setFallback(e.target.value)}
          onBlur={() => void onSave("lk_fallback_sl_pips", Number(fallback) || 0)}
          className="num ml-2 w-20 rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
        />
        <span className="ml-1 text-[11px] text-ink-3">
          pips; used only while SL parsing is off
        </span>
      </label>
    </div>
  );
}
