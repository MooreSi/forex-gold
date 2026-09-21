import { useMemo } from "react";
import { Image as ImageIcon } from "lucide-react";
import { EmptyState } from "@/components/shared/EmptyState";

interface MessageFeedSectionProps {
  messages: Record<string, unknown>[];
  total: number;
}

/**
 * The stored Telegram feed: one table per channel, all on one page.
 *
 * Asked for on 2026-09-19 as tabs ("separate tables/tabs for each channel for
 * ease of viewing"), and corrected on 2026-09-21 once they existed: "instead
 * of four tabs there should be three tables on the page each with its own
 * telegram feed to keep it simple to view on one page". Tabs answer "what is
 * this channel like" one channel at a time; side by side answers "what is each
 * channel doing right now", which is the question somebody watching three
 * signal sources actually has. There is no All tab any more either -- the
 * columns together ARE the whole feed.
 *
 * The channel column is gone with the tabs. Each table is headed by its own
 * channel, so repeating it on every row cost width that the message text
 * needed.
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

function ChannelFeed({ name, rows }: { name: string; rows: Record<string, unknown>[] }) {
  return (
    <section
      data-testid={`feed-channel-${name}`}
      className="flex min-h-0 flex-col rounded border border-line bg-surface-1"
    >
      <header className="flex shrink-0 items-baseline justify-between gap-2 border-b border-line px-2 py-1.5">
        <h3 className="truncate text-[11px] font-semibold text-ink-1" title={name}>
          {name}
        </h3>
        <span className="num shrink-0 text-[10px] text-ink-3">{rows.length}</span>
      </header>
      {/* Each column scrolls on its own. One tall page of three stacked feeds
          means scrolling past the whole of the first to reach the third. */}
      <div className="max-h-[28rem] min-h-0 overflow-auto">
        <table data-testid={`feed-table-${name}`} className="w-full text-left">
          <tbody>
            {rows.map((m, i) => (
              <Row key={String(m["id"] ?? `pos-${i}`)} row={m} />
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function MessageFeedSection({ messages, total }: MessageFeedSectionProps) {
  const channels = useMemo(() => {
    const grouped = new Map<string, Record<string, unknown>[]>();
    for (const m of messages) {
      const c = channelOf(m);
      const bucket = grouped.get(c);
      if (bucket) bucket.push(m);
      else grouped.set(c, [m]);
    }
    // Busiest first: which channel is noisy is the question this view is for.
    return [...grouped.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [messages]);

  if (messages.length === 0) {
    return (
      <EmptyState
        title="No stored messages"
        hint="Messages appear here as the reader receives them."
      />
    );
  }

  return (
    <div className="flex min-h-0 flex-col">
      <p data-testid="feed-count" className="mb-2 text-[11px] text-ink-3">
        showing <span className="num">{messages.length}</span> of{" "}
        <span className="num">{total}</span> stored, across{" "}
        <span className="num">{channels.length}</span>{" "}
        channel{channels.length === 1 ? "" : "s"}
      </p>

      {/* Side by side while there is room, stacked when there is not. Not a
          fixed three columns: the number of channels is whatever the reader
          has been given, and a hardcoded three would either squeeze a fourth
          or leave a gap. */}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {channels.map(([name, rows]) => (
          <ChannelFeed key={name} name={name} rows={rows} />
        ))}
      </div>
    </div>
  );
}
