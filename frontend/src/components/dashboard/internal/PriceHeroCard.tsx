import { ArrowDownRight, ArrowUpRight, WifiOff } from "lucide-react";
import type { Candle, HeaderState, Tick } from "@/api/types";
import {
  formatMoney, formatPrice, formatSignedMoney, pnlColour,
} from "@/components/shared/format";
import { asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";
import { dayChange } from "./dashboardMath";

interface PriceHeroCardProps {
  tick: Tick | null;
  header: HeaderState | null;
  daily: Candle[];
}

const DASH = "—";

/**
 * What gold is doing and what the account is worth: the two facts the screen
 * leads with.
 *
 * Every figure here is the backend's. The one piece of arithmetic is today's
 * move, and `dayChange` says what it is measured against and why it is an em
 * dash rather than zero when it cannot be measured.
 */
export function PriceHeroCard({ tick, header, daily }: PriceHeroCardProps) {
  const account = asObject(header?.account);
  const change = dayChange(tick?.mid, daily);
  const equity = typeof account["equity"] === "number" ? account["equity"] : null;
  const balance = typeof account["balance"] === "number" ? account["balance"] : null;
  const lifetime = header?.lifetime_pnl ?? null;
  const stale = tick == null;

  return (
    <section
      className="relative overflow-hidden rounded-lg border border-line bg-surface-1 p-4"
    >
      {/* The gold wash behind the price. Decorative and deliberately faint:
          it must never make a red number look amber. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-gradient-to-br
                   from-accent/12 via-transparent to-transparent"
      />

      <div className="relative flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span
              aria-hidden
              className="flex size-8 items-center justify-center rounded-full
                         bg-gradient-to-br from-accent to-warning text-[13px]
                         font-bold text-surface-0 shadow"
            >
              Au
            </span>
            <div>
              <p className="text-sm font-semibold leading-tight text-ink-1">XAUUSD</p>
              <p className="text-[10px] leading-tight text-ink-3">Gold / US Dollar</p>
            </div>
            {stale && (
              <span className="flex items-center gap-1 rounded bg-warning/15 px-1.5
                               py-0.5 text-[10px] text-warning">
                <WifiOff size={10} /> no price
              </span>
            )}
          </div>

          <div className="mt-2 flex flex-wrap items-baseline gap-3">
            <span
              data-testid="dash-price"
              className="num text-3xl font-bold tracking-tight text-ink-1"
            >
              {tick ? formatPrice(tick.mid) : DASH}
            </span>
            <span
              data-testid="dash-change"
              className={cn("num flex items-center gap-1 text-sm font-semibold",
                            change ? pnlColour(change.absolute) : "text-ink-3")}
              title="Against the open of today's daily candle."
            >
              {change ? (
                <>
                  {change.absolute >= 0
                    ? <ArrowUpRight size={14} /> : <ArrowDownRight size={14} />}
                  {change.absolute >= 0 ? "+" : "-"}
                  {Math.abs(change.absolute).toFixed(2)}
                  <span className="text-ink-3">
                    ({change.percent >= 0 ? "+" : "-"}
                    {Math.abs(change.percent).toFixed(2)}%)
                  </span>
                </>
              ) : (
                <>{DASH} <span className="text-ink-3">since the daily open</span></>
              )}
            </span>
          </div>

          <p className="num mt-1 text-[11px] text-ink-3">
            bid <span className="text-profit">{tick ? formatPrice(tick.bid) : DASH}</span>
            {"  ·  "}
            ask <span className="text-loss">{tick ? formatPrice(tick.ask) : DASH}</span>
            {"  ·  "}
            spread {tick && Number.isFinite(tick.spread_points)
              ? `${Math.round(tick.spread_points)}pt` : DASH}
          </p>
        </div>

        <dl className="relative grid grid-cols-3 gap-x-5 gap-y-1 text-right">
          <Figure label="Equity" value={formatMoney(equity)} />
          <Figure label="Balance" value={formatMoney(balance)} />
          <Figure
            label="Lifetime P&L"
            value={formatSignedMoney(lifetime)}
            tone={pnlColour(lifetime)}
            hint="Equity minus everything paid in: the account's whole life, including open trades, swap and commission."
          />
        </dl>
      </div>
    </section>
  );
}

function Figure({ label, value, tone, hint }: {
  label: string; value: string; tone?: string; hint?: string;
}) {
  return (
    <div title={hint}>
      <dt className="text-[9px] uppercase tracking-wider text-ink-3">{label}</dt>
      <dd className={cn("num text-sm font-semibold text-ink-1", tone)}>{value}</dd>
    </div>
  );
}
