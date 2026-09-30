import type { Trade } from "@/api/types";
import {
  formatLots, formatPrice, formatSignedMoney, pnlColour,
} from "@/components/shared/format";
import { cn } from "@/lib/cn";
import { DashCard, Pill } from "./DashCard";

/** How many rows fit before the card becomes a list to scroll. */
const SHOWN = 5;

/**
 * What is open right now, and what it is worth.
 *
 * `pnl` is the BROKER's running number for the position, passed through
 * untouched. Nothing here multiplies lots by a price difference to work out
 * what a trade is worth: that calculation has to match the broker's to the
 * penny, and a second implementation of it in the browser would not.
 *
 * A position the broker reports with no record in this app is marked
 * `untracked` — opened by hand in MetaTrader, or a record that was lost. It
 * is shown, because it is real money at risk, and labelled, because it cannot
 * be closed from here.
 *
 * Closing is not offered on this screen. The Trading tab owns that, with its
 * confirmation; a close button on a summary card is one misclick from a
 * position the operator did not mean to close.
 */
export function OpenPositionsCard({ trades }: { trades: Trade[] }) {
  const total = trades.reduce(
    (sum, t) => sum + (typeof t.pnl === "number" ? t.pnl : 0), 0);
  const anyPnl = trades.some((t) => typeof t.pnl === "number");

  return (
    <DashCard
      title="Open positions"
      icon="positions"
      badge={<Pill tone={trades.length ? "accent" : "neutral"}>
        <span data-testid="dash-open-count">{trades.length}</span> open
      </Pill>}
      actions={anyPnl ? (
        <span className={cn("num text-xs font-semibold", pnlColour(total))}>
          {formatSignedMoney(total)}
        </span>
      ) : null}
      footnote="Broker's running profit. Close from the Trading tab."
    >
      {trades.length === 0 ? (
        // One line, not a centred empty panel: this card sits above the
        // engines and the feed and should cost as little height as it can.
        <p className="py-1 text-[11px] text-ink-3">Nothing open.</p>
      ) : (
        <ul className="max-h-36 space-y-1 overflow-y-auto pr-1">
          {trades.slice(0, SHOWN).map((t, i) => (
            <li
              key={String(t.id ?? t.mt5_ticket ?? i)}
              className="flex items-center justify-between gap-2 rounded bg-surface-2
                         px-2 py-1"
            >
              <span className="flex min-w-0 items-center gap-2">
                <span className={cn(
                  "rounded px-1.5 py-0.5 text-[10px] font-semibold",
                  t.direction === "SELL"
                    ? "bg-loss/15 text-loss" : "bg-profit/15 text-profit",
                )}>
                  {String(t.direction ?? "—")}
                </span>
                <span className="num text-xs text-ink-1">{formatPrice(t.entry)}</span>
                <span className="num text-[10px] text-ink-3">
                  {formatLots(t.lots)} lots
                </span>
                {/* The source first, wherever it was opened: with the VPS
                    trading, "remote node" on every row hid which channel or
                    engine opened it. The heartbeat carries the VPS's source. */}
                <span className="truncate text-[10px] text-ink-3"
                      title={t.remote ? "Opened by the remote node" : undefined}>
                  {t.tg_source ? String(t.tg_source)
                    : t.remote ? "remote node" : t.untracked ? "untracked" : ""}
                </span>
              </span>
              <span className={cn("num shrink-0 text-xs font-semibold",
                                  pnlColour(typeof t.pnl === "number" ? t.pnl : null))}>
                {typeof t.pnl === "number" ? formatSignedMoney(t.pnl) : "—"}
              </span>
            </li>
          ))}
          {trades.length > SHOWN && (
            <li className="pt-0.5 text-[10px] text-ink-3">
              and {trades.length - SHOWN} more — the Trading tab has all of them.
            </li>
          )}
        </ul>
      )}
    </DashCard>
  );
}
