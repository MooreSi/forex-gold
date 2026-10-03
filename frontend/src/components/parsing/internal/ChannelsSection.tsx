import { useState } from "react";
import { EmptyState } from "@/components/shared/EmptyState";
import { Switch } from "@/components/shared/Switch";
import { Tooltip } from "@/components/shared/Tooltip";
import { cn } from "@/lib/cn";
import type { ParsingChannel } from "@/api/types";
import { ago, slotFor } from "./reader_status";

interface ChannelsSectionProps {
  channels: ParsingChannel[];
  onToggle: (channel: string, enabled: boolean) => Promise<void>;
  /** The reader's slots (`reader.slots`), to show whether each is listening. */
  slots?: Record<string, unknown>[];
}

/**
 * Which channels the parser reads at all, and whether each one's listener is
 * actually up.
 *
 * **These boxes did nothing at all until 2026-09-21.** The endpoint behind
 * them called the config writer with two arguments where it takes six, so
 * every click raised a TypeError, answered 500, and the box sprang back on the
 * next poll with nothing on screen to say why. The fix is in
 * `services/channels/performance.set_parser_enabled`; what belongs here is
 * that a refused write is now SAID, rather than silently undone — that silence
 * is what made a hard 500 look like a checkbox that would not stick.
 *
 * The listener line (2026-09-30) answers the other half of "why did this
 * channel not trade": parsing on and nobody listening looks the same from the
 * switch alone.
 */
export function ChannelsSection({ channels, onToggle, slots = [] }: ChannelsSectionProps) {
  const [failed, setFailed] = useState<Record<string, string>>({});

  const toggle = async (name: string, enabled: boolean) => {
    setFailed((f) => ({ ...f, [name]: "" }));
    try {
      await onToggle(name, enabled);
    } catch (e) {
      setFailed((f) => ({
        ...f,
        [name]: e instanceof Error ? e.message : String(e),
      }));
    }
  };

  if (channels.length === 0) {
    return (
      <EmptyState
        title="No channels configured"
        hint="Add the channels to read under Settings → Telegram, then they appear here."
      />
    );
  }
  return (
    <ul className="grid gap-2 lg:grid-cols-2">
      {channels.map((c) => {
        const on = c.parser["enabled"] !== false && c.parser["enabled"] !== 0;
        const slot = slotFor(slots, c.name);
        const listening = slot ? Boolean(slot["listener_active"] || slot["poller_active"]) : null;
        const pollError = slot?.["last_poll_error"] ? String(slot["last_poll_error"]) : "";
        return (
          <li
            key={c.name}
            data-testid={`channel-${c.name}`}
            className={cn(
              "rounded-md border bg-surface-2 px-3 py-2.5",
              on ? "border-line" : "border-line opacity-80",
            )}
          >
            <div className="flex items-center gap-3">
              <Switch
                checked={on}
                onChange={(v) => void toggle(c.name, v)}
                label={`Parse ${c.name}`}
                help={
                  on
                    ? `Messages from ${c.name} are being read and parsed into signals. `
                      + "Switch off to stop reading it — the messages are still stored, they "
                      + "are just not turned into signals."
                    : `Messages from ${c.name} are stored but NOT parsed. Switch on to start `
                      + "turning them into signals again."
                }
              />
              <span className="min-w-0 truncate text-xs font-semibold text-ink-1">{c.name}</span>
              {Array.isArray(c.parser["learned"]) && (
                <Tooltip label="Rules taught to the parser from this channel's own messages, on the Questions tab.">
                  <span className="num ml-auto shrink-0 rounded bg-surface-3 px-1.5 py-0.5 text-[10px] text-ink-2">
                    {(c.parser["learned"] as unknown[]).length} learned rules
                  </span>
                </Tooltip>
              )}
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 pl-10 text-[11px]">
              <span className={on ? "text-profit" : "text-ink-3"}>
                {on ? "Parsing into signals" : "Stored only, not parsed"}
              </span>
              {listening !== null && (
                <span className={listening ? "text-ink-2" : "text-warning"}>
                  {listening ? "Listening" : "Listener down"}
                  {typeof slot?.["last_poll_at"] === "string" &&
                    ` · checked ${ago(slot["last_poll_at"] as string)}`}
                </span>
              )}
              {listening === null && slots.length > 0 && (
                <span className="text-warning">Not assigned to a reader slot</span>
              )}
            </div>
            {pollError && <p className="mt-1 pl-10 text-[10px] text-warning">{pollError}</p>}
            {failed[c.name] && (
              <p role="alert" className="mt-1 pl-10 text-[10px] text-loss">
                {failed[c.name]}
              </p>
            )}
          </li>
        );
      })}
    </ul>
  );
}
