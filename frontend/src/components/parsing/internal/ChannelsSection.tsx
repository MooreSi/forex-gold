import { useState } from "react";
import { EmptyState } from "@/components/shared/EmptyState";
import { Tooltip } from "@/components/shared/Tooltip";
import type { ParsingChannel } from "@/api/types";

interface ChannelsSectionProps {
  channels: ParsingChannel[];
  onToggle: (channel: string, enabled: boolean) => Promise<void>;
}

/**
 * Which channels the parser reads at all.
 *
 * **These boxes did nothing at all until 2026-09-21.** The endpoint behind
 * them called the config writer with two arguments where it takes six, so
 * every click raised a TypeError, answered 500, and the box sprang back on the
 * next poll with nothing on screen to say why. The fix is in
 * `services/channels/performance.set_parser_enabled`; what belongs here is
 * that a refused write is now SAID, rather than silently undone — that silence
 * is what made a hard 500 look like a checkbox that would not stick.
 */
export function ChannelsSection({ channels, onToggle }: ChannelsSectionProps) {
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
    <ul className="space-y-1.5">
      {channels.map((c) => {
        const on = c.parser["enabled"] !== false && c.parser["enabled"] !== 0;
        return (
          <li
            key={c.name}
            data-testid={`channel-${c.name}`}
            className="rounded border border-line bg-surface-2 px-3 py-2"
          >
            <div className="flex items-center gap-3">
              <Tooltip
                label={
                  on
                    ? `Messages from ${c.name} are being read and parsed into signals. `
                      + "Untick to stop reading it — the messages are still stored, they "
                      + "are just not turned into signals."
                    : `Messages from ${c.name} are stored but NOT parsed. Tick to start `
                      + "turning them into signals again."
                }
              >
                <input
                  type="checkbox"
                  aria-label={`Parse ${c.name}`}
                  checked={on}
                  onChange={(e) => void toggle(c.name, e.target.checked)}
                  className="accent-accent"
                />
              </Tooltip>
              <span className="text-xs text-ink-1">{c.name}</span>
              {Array.isArray(c.parser["learned"]) && (
                <Tooltip label="Rules taught to the parser from this channel's own messages, on the Questions tab.">
                  <span className="num ml-auto text-[10px] text-ink-3">
                    {(c.parser["learned"] as unknown[]).length} learned rules
                  </span>
                </Tooltip>
              )}
            </div>
            {failed[c.name] && (
              <p role="alert" className="mt-1 text-[10px] text-loss">
                {failed[c.name]}
              </p>
            )}
          </li>
        );
      })}
    </ul>
  );
}
