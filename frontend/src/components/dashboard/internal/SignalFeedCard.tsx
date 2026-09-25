import { EmptyState } from "@/components/shared/EmptyState";
import { formatPrice, formatSignedMoney } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import { DashCard, Pill } from "./DashCard";

/** How many rows fit in a card this size. The Trading tab has the rest. */
const SHOWN = 6;

/**
 * What a row is called, and the colour that carries it.
 *
 * Four words the operator uses, plus `flat`. `closed` is not among them on
 * purpose: it was the same word for a signal that took 300 dollars and one
 * that gave back 300, which is 532 of the owner's 608 rows saying nothing.
 *
 * The colours are the app's frozen meanings and are not chosen freshly here.
 * Green is money made and red is money lost, so **skipped must not be red** --
 * a column of red "skipped" rows reads as a losing run when nothing was
 * traded at all. It is neutral grey. And **open must not be green**: it has
 * won nothing yet, so it takes the app's accent instead.
 */
const LABEL_TONE: Record<string, string> = {
  won: "bg-profit/15 text-profit",
  lost: "bg-loss/15 text-loss",
  flat: "bg-surface-3 text-ink-2",
  open: "bg-accent/15 text-accent",
  skipped: "bg-surface-3 text-ink-3",
};

/** Statuses that mean "this never became a trade". */
const SKIPPED_STATUSES = new Set(["expired", "cancelled", "rejected"]);
/** Statuses that mean "this is live now". */
const OPEN_STATUSES = new Set(["active", "open", "filled"]);

/**
 * The word for one signal.
 *
 * **The outcome wins over the status whenever there is one.** Three signals
 * in the owner's database are `cancelled` and still have a winning trade
 * against them -- the signal was withdrawn after the position was taken. The
 * money happened, so the money is the answer; "skipped" would hide a real
 * win.
 *
 * A status this does not recognise is returned AS ITSELF rather than forced
 * into one of the four. `pending` is reachable and is neither open nor
 * skipped -- it may still be taken -- and inventing a label for a state
 * nobody has described is how a screen starts lying quietly.
 */
export function signalLabel(outcome: string, status: string): string {
  if (outcome === "won" || outcome === "lost" || outcome === "flat") return outcome;
  if (OPEN_STATUSES.has(status)) return "open";
  if (SKIPPED_STATUSES.has(status)) return "skipped";
  return status;
}

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
            const label = signalLabel(text(s, "outcome").toLowerCase(), status);
            // The figure behind the word, for the operator who wants to know
            // how big a win it was. On the title rather than in the row: six
            // of these in a narrow card is a wall of numbers, and the label
            // is what the card is scanned for.
            const netPnl = price(s, "net_pnl");
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
                  {label && (
                    <span
                      title={netPnl == null
                        ? undefined
                        : `Realised ${formatSignedMoney(netPnl)}`}
                      className={cn(
                        "rounded px-1.5 py-0.5 text-[9px] uppercase tracking-wide",
                        LABEL_TONE[label] ?? "bg-surface-3 text-ink-3",
                      )}
                    >
                      {label}
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
