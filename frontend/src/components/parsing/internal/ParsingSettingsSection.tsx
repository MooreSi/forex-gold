import { useEffect, useState } from "react";
import { Tooltip } from "@/components/shared/Tooltip";
import { cn } from "@/lib/cn";
import { PARSING_CATEGORIES } from "../content/settings";
import { ParsingToggleCard } from "./ParsingToggleCard";
import { RealignLimitField } from "./RealignLimitField";

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
      {PARSING_CATEGORIES.map((category) => {
        const nOn = category.toggles.filter((t) => on(settings, t.key, t.defaultOn)).length;
        return (
          <section key={category.badge} className="rounded-lg border border-line bg-surface-1 p-3">
            <header className="mb-2.5 flex flex-wrap items-center gap-2">
              <span
                className={cn(
                  "rounded px-1.5 py-0.5 text-[10px] font-bold tracking-wider",
                  TONE_CLASS[category.tone],
                )}
              >
                {category.badge}
              </span>
              <span className="text-[11px] text-ink-3">{category.summary}</span>
              <span className="num ml-auto text-[10px] text-ink-3">
                {nOn} of {category.toggles.length} on
              </span>
            </header>
            <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
              {category.toggles.map((t) => (
                <ParsingToggleCard
                  key={t.key}
                  toggle={t}
                  on={on(settings, t.key, t.defaultOn)}
                  blocks={category.tone === "warning"}
                  onChange={(v) => void onSave(t.key, v ? 1 : 0)}
                >
                  {t.key === "lk_entry_realignment" && (
                    <RealignLimitField
                      value={settings["lk_entry_realignment_max_pips"]}
                      onSave={(pips) => onSave("lk_entry_realignment_max_pips", pips)}
                    />
                  )}
                </ParsingToggleCard>
              ))}
            </div>
          </section>
        );
      })}
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

  const field =
    "num w-20 rounded border border-line bg-surface-2 px-2 py-1 text-right text-ink-1 focus:border-accent focus:outline-none";

  return (
    <section className="rounded-lg border border-line bg-surface-1 p-3">
      <header className="mb-2.5 flex items-center gap-2">
        <span className="rounded bg-surface-3 px-1.5 py-0.5 text-[10px] font-bold tracking-wider text-ink-2">
          VALUES
        </span>
        <span className="text-[11px] text-ink-3">Saved when you leave the field.</span>
      </header>
      <div className="grid gap-2 md:grid-cols-2">
        <label className="flex items-center justify-between gap-3 rounded-md border border-line bg-surface-2 p-3 text-xs text-ink-1">
          <span>
            <span className="font-semibold">Second-message match window</span>
            <span className="block text-[11px] text-ink-3">
              Used by TP/SL in Second Message
            </span>
          </span>
          <span className="flex items-center gap-1.5">
            <Tooltip label="How long after a signal a follow-up message can still be matched to it — an SL or a TP sent in a second post. Longer windows catch more follow-ups and risk attaching one to the wrong signal.">
              <input
                aria-label="Second-message match window"
                inputMode="numeric"
                value={window}
                onChange={(e) => setWindow(e.target.value)}
                onBlur={() => void onSave("lk_second_message_match_window_sec", Number(window) || 0)}
                className={field}
              />
            </Tooltip>
            <span className="text-[11px] text-ink-3">sec</span>
          </span>
        </label>
        <label className="flex items-center justify-between gap-3 rounded-md border border-line bg-surface-2 p-3 text-xs text-ink-1">
          <span>
            <span className="font-semibold">Fallback SL distance</span>
            <span className="block text-[11px] text-ink-3">
              Used only while SL parsing is off
            </span>
          </span>
          <span className="flex items-center gap-1.5">
            <Tooltip label="The stop used when a signal names none and SL parsing is switched off. It decides how much a trade from such a signal can lose.">
              <input
                aria-label="Fallback SL distance"
                inputMode="decimal"
                value={fallback}
                onChange={(e) => setFallback(e.target.value)}
                onBlur={() => void onSave("lk_fallback_sl_pips", Number(fallback) || 0)}
                className={field}
              />
            </Tooltip>
            <span className="text-[11px] text-ink-3">pips</span>
          </span>
        </label>
      </div>
    </section>
  );
}
