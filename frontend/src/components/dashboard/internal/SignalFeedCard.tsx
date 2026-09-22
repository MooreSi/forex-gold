import { EmptyState } from "@/components/shared/EmptyState";
import { formatPrice } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import { DashCard, Pill } from "./DashCard";

/** How many rows fit in a card this size. The Trading tab has the rest. */
const SHOWN = 6;

const STATUS_TONE: Record<string, string> = {
  open: "bg-profit/15 text-profit",
  filled: "bg-profit/15 text-profit",
  pending: "bg-warning/15 text-warning",
  cancelled: "bg-surface-3 text-ink-3",
  closed: "bg-surface-3 text-ink-3",
  rejected: "bg-loss/15 text-loss",
};

function text(row: Record<string, unknown>, key: string): string {
  const raw = row[key];
  return raw == null || raw === "" ? "" : String(raw);
}

function price(row: Record<string, unknown>, key: string): number | null {
  const raw = row[key];
  return typeof raw === "number" ? raw : null;
}

/**
 * The newest signals, whatever produced them.
 *
 * These are the rows the Trading tab's Signals table draws, from the same
 * poll key: a signal parsed from a Telegram channel and one raised by this
 * app's own engines are the same kind of thing by the time they get here, and
 * the source column is what tells them apart.
 *
 * Status is shown because a signal that exists and a signal that was TAKEN
 * are different facts, and a feed that shows only the first reads as a
 * trading record it is not.
 */
export function SignalFeedCard({ signals }: { signals: Record<string, unknown>[] }) {
  return (
    <DashCard
      title="Signal feed"
      icon="signals"
      badge={<Pill>{signals.length} recent</Pill>}
      footnote="Parsed channels and this app's own engines. Edit or cancel one from the Trading tab."
    >
      {signals.length === 0 ? (
        <EmptyState
          title="No signals yet"
          hint="Parsed Telegram signals and engine signals both land here."
        />
      ) : (
        <ul className="space-y-1">
          {signals.slice(0, SHOWN).map((s, i) => {
            const direction = text(s, "direction").toUpperCase();
            const status = text(s, "status").toLowerCase();
            const entry = price(s, "entry");
            return (
              <li
                key={text(s, "id") || i}
                className="flex items-center justify-between gap-2 border-b
                           border-line/60 pb-1 last:border-0"
              >
                <span className="flex min-w-0 items-center gap-2">
                  <span className={cn(
                    "rounded px-1.5 py-0.5 text-[10px] font-semibold",
                    direction === "SELL"
                      ? "bg-loss/15 text-loss" : "bg-profit/15 text-profit",
                  )}>
                    {direction || "—"}
                  </span>
                  <span className="truncate text-[11px] text-ink-2">
                    {text(s, "source") || "unnamed source"}
                  </span>
                </span>
                <span className="flex shrink-0 items-center gap-2">
                  <span className="num text-[11px] text-ink-1">
                    {entry == null ? "—" : formatPrice(entry)}
                  </span>
                  {status && (
                    <span className={cn(
                      "rounded px-1.5 py-0.5 text-[9px] uppercase tracking-wide",
                      STATUS_TONE[status] ?? "bg-surface-3 text-ink-3",
                    )}>
                      {status}
                    </span>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </DashCard>
  );
}
