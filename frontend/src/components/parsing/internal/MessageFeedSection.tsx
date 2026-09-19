import { useMemo, useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { Image as ImageIcon } from "lucide-react";
import { EmptyState } from "@/components/shared/EmptyState";
import { cn } from "@/lib/cn";

interface MessageFeedSectionProps {
  messages: Record<string, unknown>[];
  total: number;
}

/**
 * The stored Telegram feed, one table per channel.
 *
 * Asked for on 2026-09-19: "separate tables/tabs for each channel for ease of
 * viewing". One mixed stream answers "what arrived"; it does not answer "what
 * is this channel like", which is the question anybody scrolling this has.
 *
 * **Rows are keyed on the message id, never on position.** That is the other
 * half of the owner's report that the feed "appears to be updating when there
 * are no new messages": `fetch_stored_messages` was not selecting `id`, so the
 * key fell back to the array index, and one arrival at the top shifted every
 * key and made React rewrite the whole list. The backend now returns the
 * column; this side must not quietly fall back to the index again.
 *
 * Channel comes from `group_name`. A row without one is grouped under a single
 * honest label rather than dropped or merged into whichever channel sorted
 * first.
 */
const NO_CHANNEL = "unknown channel";
const ALL = "__all__";

function channelOf(row: Record<string, unknown>): string {
  const raw = row["group_name"] ?? row["channel"];
  return typeof raw === "string" && raw.trim() ? raw : NO_CHANNEL;
}

function textOf(row: Record<string, unknown>): string {
  const raw = row["text"] ?? row["raw_text"];
  return typeof raw === "string" ? raw : "";
}

/**
 * When the message was sent, rendered in UK local time.
 *
 * `telegram_messages.timestamp` is a TEXT column holding an ISO 8601 string
 * with an explicit UTC offset -- "2026-09-18T16:26:44+00:00" -- not an epoch.
 * The previous helper accepted only a number, so every row in the feed showed
 * an em dash where its time should be. Checked against the live payload,
 * 2026-09-19.
 *
 * **Not `formatBrokerTime`.** These stamps are real UTC from Telegram, not
 * MT5 broker time, so subtracting the three-hour broker offset would put every
 * message three hours early. The broker shift belongs to deal history only.
 */
function stamp(row: Record<string, unknown>): string {
  const raw = row["timestamp"] ?? row["received_at"] ?? row["ts"];
  const ms = typeof raw === "number" ? raw * 1000
    : typeof raw === "string" ? Date.parse(raw)
    : NaN;
  if (!Number.isFinite(ms)) return "—";
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
    timeZone: "Europe/London",
  }).format(new Date(ms));
}

function Row({ row }: { row: Record<string, unknown> }) {
  const text = textOf(row);
  const media = row["has_media"] ? String(row["media_type"] ?? "media") : null;

  return (
    <tr
      data-testid={`feed-row-${String(row["id"] ?? "")}`}
      className="border-t border-line align-top"
    >
      <td className="num whitespace-nowrap px-2 py-1.5 text-[10px] text-ink-3">
        {stamp(row)}
      </td>
      <td className="whitespace-nowrap px-2 py-1.5 text-[11px] text-ink-2">
        {channelOf(row)}
      </td>
      <td className="px-2 py-1.5 text-xs text-ink-1">
        <span className="whitespace-pre-wrap">{text}</span>
        {media && (
          // A photo-only post is a real message with no text. Rendered bare it
          // is an empty row that reads as a parsing failure.
          <span className="ml-1 inline-flex items-center gap-1 rounded bg-surface-3 px-1.5 py-0.5 text-[10px] text-ink-3">
            <ImageIcon size={10} /> {media}
          </span>
        )}
      </td>
    </tr>
  );
}

export function MessageFeedSection({ messages, total }: MessageFeedSectionProps) {
  const [tab, setTab] = useState(ALL);

  const channels = useMemo(() => {
    const counts = new Map<string, number>();
    for (const m of messages) {
      const c = channelOf(m);
      counts.set(c, (counts.get(c) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [messages]);

  if (messages.length === 0) {
    return (
      <EmptyState
        title="No stored messages"
        hint="Messages appear here as the reader receives them."
      />
    );
  }

  const shown = tab === ALL ? messages : messages.filter((m) => channelOf(m) === tab);

  return (
    <div className="flex min-h-0 flex-col">
      <p data-testid="feed-count" className="mb-2 text-[11px] text-ink-3">
        showing <span className="num">{shown.length}</span> of{" "}
        <span className="num">{total}</span> stored
      </p>

      <Tabs.Root value={tab} onValueChange={setTab} className="flex min-h-0 flex-col">
        <Tabs.List className="mb-2 flex flex-wrap gap-1 border-b border-line">
          <Tabs.Trigger value={ALL} className={TRIGGER}>
            All channels
            <span className="num ml-1.5 text-ink-3">{messages.length}</span>
          </Tabs.Trigger>
          {channels.map(([name, count]) => (
            <Tabs.Trigger key={name} value={name} className={TRIGGER}>
              {name}
              <span className="num ml-1.5 text-ink-3">{count}</span>
            </Tabs.Trigger>
          ))}
        </Tabs.List>

        <div className="min-h-0 overflow-auto">
          <table data-testid="feed-table" className="w-full text-left">
            <tbody>
              {shown.map((m, i) => (
                <Row key={String(m["id"] ?? `pos-${i}`)} row={m} />
              ))}
            </tbody>
          </table>
        </div>
      </Tabs.Root>
    </div>
  );
}

const TRIGGER = cn(
  "-mb-px whitespace-nowrap border-b-2 px-2.5 py-1 text-[11px] transition-colors",
  "border-transparent text-ink-3 hover:text-ink-2",
  "data-[state=active]:border-accent data-[state=active]:text-ink-1",
);
