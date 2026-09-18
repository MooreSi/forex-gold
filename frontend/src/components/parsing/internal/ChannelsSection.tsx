import { EmptyState } from "@/components/shared/EmptyState";
import type { ParsingChannel } from "@/api/types";

interface ChannelsSectionProps {
  channels: ParsingChannel[];
  onToggle: (channel: string, enabled: boolean) => Promise<void>;
}

/** Which channels the parser reads at all. */
export function ChannelsSection({ channels, onToggle }: ChannelsSectionProps) {
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
      {channels.map((c) => (
        <li
          key={c.name}
          className="flex items-center gap-3 rounded border border-line bg-surface-2 px-3 py-2"
        >
          <input
            type="checkbox"
            aria-label={`Parse ${c.name}`}
            checked={c.parser["enabled"] !== false}
            onChange={(e) => void onToggle(c.name, e.target.checked)}
            className="accent-accent"
          />
          <span className="text-xs text-ink-1">{c.name}</span>
          {Array.isArray(c.parser["learned"]) && (
            <span className="num ml-auto text-[10px] text-ink-3">
              {(c.parser["learned"] as unknown[]).length} learned rules
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}
