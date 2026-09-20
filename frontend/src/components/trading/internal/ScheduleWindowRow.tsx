import { useState } from "react";
import { ChevronDown, ChevronRight, SlidersHorizontal } from "lucide-react";
import { asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";

export interface OverrideChoice { value: string; label: string }

interface ScheduleWindowRowProps {
  day: string;
  dayLabel: string;
  index: number;
  block: Record<string, unknown>;
  channels: string[];
  options: OverrideChoice[];
  onPatch: (patch: Record<string, unknown>) => void;
}

const ENGINES: { key: string; label: string }[] = [
  { key: "reversal_engine", label: "Reversal Engine" },
  { key: "breakout_engine", label: "Breakout Engine" },
];

function channelCfg(block: Record<string, unknown>, name: string) {
  const all = asObject(block["telegram_channels"]);
  return asObject(all[name]);
}

/**
 * One trading window: when it runs, what it may earn, and who may trade in it.
 *
 * The Channels panel is the part the React port never had. Each Telegram
 * channel and each internal engine can be allowed or blocked for this window
 * independently, and each carries its own strategy Override — so two channels
 * can run different strategies inside the same hours. Unchecking one blocks
 * only that source's live execution; the others are unaffected.
 */
export function ScheduleWindowRow({
  day, dayLabel, index, block, channels, options, onPatch,
}: ScheduleWindowRowProps) {
  const [open, setOpen] = useState(false);
  const enabled = block["enabled"] === true;
  const tgDefault = block["telegram_default_enabled"] !== false;

  const enabledCount =
    channels.filter((c) => {
      const cfg = channelCfg(block, c);
      return cfg["enabled"] === undefined ? tgDefault : cfg["enabled"] === true;
    }).length + ENGINES.filter((e) => block[e.key] !== false).length;
  const total = channels.length + ENGINES.length;

  const patchChannel = (name: string, patch: Record<string, unknown>) => {
    const all = { ...asObject(block["telegram_channels"]) };
    all[name] = { ...asObject(all[name]), ...patch };
    onPatch({ telegram_channels: all });
  };

  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <div
      data-testid={`window-${day}-${index}`}
      className={cn("rounded border px-2 py-1.5",
        enabled ? "border-line bg-surface-2" : "border-line/50 bg-surface-1")}
    >
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="checkbox"
          aria-label={`${dayLabel} window ${index + 1}`}
          checked={enabled}
          onChange={(e) => onPatch({ enabled: e.target.checked })}
          className="accent-accent"
        />
        <span className="w-16 text-[11px] text-ink-3">Window {index + 1}</span>
        <input
          aria-label={`${dayLabel} window ${index + 1} start`}
          defaultValue={String(block["start"] ?? "")}
          onBlur={(e) => onPatch({ start: e.target.value })}
          className="num w-14 rounded border border-line bg-surface-1 px-1 py-0.5 text-[11px] text-ink-1"
        />
        <span className="text-ink-3">–</span>
        <input
          aria-label={`${dayLabel} window ${index + 1} end`}
          defaultValue={String(block["end"] ?? "")}
          onBlur={(e) => onPatch({ end: e.target.value })}
          className="num w-14 rounded border border-line bg-surface-1 px-1 py-0.5 text-[11px] text-ink-1"
        />
        <label className="flex items-center gap-1 text-[11px] text-ink-3">
          Target $
          <input
            aria-label={`${dayLabel} window ${index + 1} target`}
            inputMode="decimal"
            defaultValue={String(block["target"] ?? 0)}
            onBlur={(e) => onPatch({ target: Number(e.target.value) || 0 })}
            title="0 = no profit cap, only the time window applies"
            className="num w-16 rounded border border-line bg-surface-1 px-1 py-0.5 text-ink-1"
          />
        </label>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="ml-auto flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-ink-3 hover:text-ink-1"
        >
          <Chevron className="h-3 w-3" aria-hidden />
          <SlidersHorizontal className="h-3 w-3" aria-hidden />
          Channels ({enabledCount}/{total})
        </button>
      </div>

      {open && (
        <div className="mt-2 space-y-1.5 rounded bg-surface-1 p-2">
          <label className="flex items-center gap-2 text-[11px] text-ink-3">
            <input
              type="checkbox"
              checked={tgDefault}
              onChange={(e) => onPatch({ telegram_default_enabled: e.target.checked })}
              className="accent-accent"
            />
            New or unlisted channels default to enabled
          </label>

          {channels.map((name) => {
            const cfg = channelCfg(block, name);
            const on = cfg["enabled"] === undefined ? tgDefault : cfg["enabled"] === true;
            return (
              <div key={name} className="flex flex-wrap items-center gap-2">
                <label className="flex w-48 items-center gap-2 text-[11px] text-ink-2">
                  <input
                    type="checkbox"
                    aria-label={`${name} in ${dayLabel} window ${index + 1}`}
                    checked={on}
                    onChange={(e) => patchChannel(name, { enabled: e.target.checked })}
                    className="accent-accent"
                  />
                  <span className="truncate">{name}</span>
                </label>
                <select
                  aria-label={`${name} override in ${dayLabel} window ${index + 1}`}
                  value={String(cfg["strategy_override"] ?? "")}
                  onChange={(e) => patchChannel(name, { strategy_override: e.target.value })}
                  className="w-44 rounded border border-line bg-surface-2 px-1 py-0.5 text-[11px] text-ink-1"
                >
                  {options.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
              </div>
            );
          })}

          <p className="pt-1 text-[10px] uppercase tracking-wide text-ink-3">
            Internal signal generators
          </p>
          {ENGINES.map(({ key, label }) => (
            <div key={key} className="flex flex-wrap items-center gap-2">
              <label className="flex w-48 items-center gap-2 text-[11px] text-ink-2">
                <input
                  type="checkbox"
                  aria-label={`${label} in ${dayLabel} window ${index + 1}`}
                  checked={block[key] !== false}
                  onChange={(e) => onPatch({ [key]: e.target.checked })}
                  className="accent-accent"
                />
                {label}
              </label>
              <select
                aria-label={`${label} override in ${dayLabel} window ${index + 1}`}
                value={String(block[`${key}_override`] ?? "")}
                onChange={(e) => onPatch({ [`${key}_override`]: e.target.value })}
                className="w-44 rounded border border-line bg-surface-2 px-1 py-0.5 text-[11px] text-ink-1"
              >
                {options.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
