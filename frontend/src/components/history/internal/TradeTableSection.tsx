import { useCallback } from "react";
import { api } from "@/api/client";
import { EmptyState } from "@/components/shared/EmptyState";
import {
  formatBrokerTime, formatMoney, formatPrice, pnlColour,
} from "@/components/shared/format";
import { asArray } from "@/lib/asArray";
import { usePoll } from "@/hooks/usePoll";

interface TradeRow {
  ticket: number;
  direction: string;
  entry_price: number;
  exit_price: number;
  open_ts: number;
  close_ts: number;
  lots: number;
  close_lots: number[];
  pnl: number;
  fees: number;
  pips: number | null;
  duration_secs: number | null;
  order_type: string;
  pending_secs: number | null;
  reason: string;
  source: string;
  strategy: string;
  max_tp: string;
  rr: number | null;
  spread_points: number | null;
  group: [string, number] | null;
}

interface TradesState { rows: TradeRow[]; error: string | null }

/**
 * Every closed trade in the window, deal by deal.
 *
 * The last thing the React port could not build: the NiceGUI page read
 * `engine._bridge.get_deal_history()` directly, which the controller boundary
 * forbids. `get_deal_history` went onto the runtime facade on 2026-09-19 with
 * the owner's sign-off — `facade_baseline.json` records it, 89 to 90.
 *
 * Built from **MT5's own record**, so a trade opened by hand in the terminal
 * or by the copier EA appears here even though it never had a local row. The
 * attribution columns — channel, strategy, max TP, R:R — come from the local
 * database and are merged on by the backend.
 *
 * Three columns say "nothing yet" rather than a number, and each is deliberate:
 * a blank **Max TP** means the 30-minute window has not elapsed, "…" means it
 * has and the sweep has not caught up, and an em dash in **pips** or
 * **duration** means the opening deal is outside the window — not that the
 * trade scratched.
 */
function duration(secs: number | null): string {
  if (secs == null || !Number.isFinite(secs) || secs <= 0) return "—";
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h}h ${m % 60}m` : `${Math.floor(h / 24)}d ${h % 24}h`;
}

function lots(row: TradeRow): string {
  const closes = asArray<number>(row.close_lots);
  // A partial close is several exit deals. One number would hide that the
  // position came off in pieces, which is the thing a strategy review is for.
  return closes.length > 1
    ? `${row.lots.toFixed(2)} (${closes.map((v) => v.toFixed(2)).join(" + ")})`
    : row.lots.toFixed(2);
}

const COLUMNS = [
  "Closed", "Ticket", "Side", "Entry", "Exit", "Lots", "P&L", "Pips",
  "Cost", "Spread", "Held", "Order", "Pending", "Max TP", "R:R", "Channel",
  "Strategy", "Reason",
];

export function TradeTableSection({ days }: { days: number }) {
  const poll = usePoll<TradesState>(
    `history/trades/${days}`,
    useCallback(() => api.get<TradesState>(`/api/history/trades?days=${days}`), [days]),
    60_000,
  );

  const data = poll.data;
  if (!data) {
    return <EmptyState title={poll.error ? "Could not load the trades" : "Loading"}
      hint={poll.error?.message} />;
  }

  // Separate from an empty list on purpose: "no trades in this window" and
  // "the bridge is down" look identical in an empty table and call for
  // completely different responses.
  if (data.error) {
    return <EmptyState title="No broker data" hint={data.error} />;
  }

  const rows = asArray<TradeRow>(data.rows);
  if (rows.length === 0) {
    return <EmptyState title="No trades closed in this window" />;
  }

  return (
    <div className="overflow-auto" data-testid="trade-table">
      <p className="mb-2 text-[11px] text-ink-3">
        {rows.length} closed {rows.length === 1 ? "trade" : "trades"}, from MT5's
        own deal history — including any opened outside this app.
      </p>
      <table className="w-full min-w-[60rem] text-left text-[11px]">
        <thead className="text-ink-3">
          <tr>{COLUMNS.map((c) => <th key={c} className="px-2 py-1 font-normal">{c}</th>)}</tr>
        </thead>
        <tbody className="num">
          {rows.map((row) => {
            const group = row.group ? `${row.group[0]} · leg ${row.group[1]}` : null;
            return (
              <tr
                key={row.ticket}
                data-testid={`trade-${row.ticket}`}
                className="border-t border-line hover:bg-surface-2"
              >
                {/* A deal stamp is broker time (UTC+3). Rendered raw it puts
                    every trade three hours into the future, which looks
                    entirely plausible on a table of closes. */}
                <td className="px-2 py-1 text-ink-3">{formatBrokerTime(row.close_ts)}</td>
                <td className="px-2 py-1 text-ink-3">{row.ticket}</td>
                <td className={`px-2 py-1 ${row.direction === "BUY" ? "text-profit" : "text-loss"}`}>
                  {row.direction}
                </td>
                <td className="px-2 py-1">{row.entry_price ? formatPrice(row.entry_price) : "—"}</td>
                <td className="px-2 py-1">{formatPrice(row.exit_price)}</td>
                <td className="px-2 py-1">{lots(row)}</td>
                <td className={`px-2 py-1 ${pnlColour(row.pnl)}`}>{formatMoney(row.pnl)}</td>
                <td className={`px-2 py-1 ${row.pips == null ? "text-ink-3" : pnlColour(row.pips)}`}>
                  {row.pips == null ? "—" : `${row.pips >= 0 ? "+" : ""}${row.pips.toFixed(1)}`}
                </td>
                {/* Already inside `pnl` via MT5's real fill prices. Shown as
                    a breakdown, never subtracted a second time. */}
                <td className="px-2 py-1 text-ink-3">{formatMoney(row.fees)}</td>
                <td className="px-2 py-1 text-ink-3">
                  {row.spread_points == null ? "—" : `${row.spread_points.toFixed(1)}pt`}
                </td>
                <td className="px-2 py-1 text-ink-3">{duration(row.duration_secs)}</td>
                <td className="px-2 py-1 text-ink-3">{row.order_type}</td>
                <td className="px-2 py-1 text-ink-3">{duration(row.pending_secs)}</td>
                <td className="px-2 py-1 text-ink-3">{row.max_tp || "—"}</td>
                <td className="px-2 py-1 text-ink-3">
                  {row.rr == null ? "—" : `${row.rr.toFixed(2)}:1`}
                </td>
                <td className="px-2 py-1 text-ink-2" title={group ?? undefined}>
                  {row.source || "—"}
                </td>
                <td className="px-2 py-1 text-ink-2">{row.strategy || "—"}</td>
                <td className="px-2 py-1 text-ink-3">{row.reason || "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
