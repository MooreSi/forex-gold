import { EmptyState } from "@/components/shared/EmptyState";
import { formatClock } from "@/components/shared/format";

interface MessageFeedSectionProps {
  messages: Record<string, unknown>[];
  total: number;
}

function ts(row: Record<string, unknown>): number | null {
  const raw = row["timestamp"] ?? row["received_at"] ?? row["ts"];
  return typeof raw === "number" ? raw : null;
}

export function MessageFeedSection({ messages, total }: MessageFeedSectionProps) {
  if (messages.length === 0) {
    return (
      <EmptyState
        title="No stored messages"
        hint="Messages appear here as the reader receives them."
      />
    );
  }
  return (
    <div>
      <p className="mb-2 text-[11px] text-ink-3">
        showing <span className="num">{messages.length}</span> of{" "}
        <span className="num">{total}</span>
      </p>
      <ul className="space-y-1">
        {messages.map((m, i) => (
          <li key={String(m["id"] ?? i)} className="rounded border border-line bg-surface-2 px-3 py-1.5">
            <div className="flex items-baseline gap-2">
              <span className="num text-[10px] text-ink-3">{formatClock(ts(m))}</span>
              <span className="text-[11px] text-ink-2">
                {String(m["group_name"] ?? m["channel"] ?? "—")}
              </span>
            </div>
            <p className="whitespace-pre-wrap text-xs text-ink-1">
              {String(m["text"] ?? m["raw_text"] ?? "")}
            </p>
          </li>
        ))}
      </ul>
    </div>
  );
}
