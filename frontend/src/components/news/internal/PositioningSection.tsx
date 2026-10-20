import { StatCard } from "@/components/shared/StatCard";
import { cn } from "@/lib/cn";
import { usePositioning } from "../hooks/usePositioning";

const whole = (v: number | null | undefined, signed = false) =>
  v == null ? "—" : `${signed && v > 0 ? "+" : ""}${v.toLocaleString("en-US")}`;
const tonnes = (v: number | null | undefined, signed = false) =>
  v == null ? "—"
    : `${signed && v > 0 ? "+" : ""}${v.toLocaleString("en-US", {
      minimumFractionDigits: signed ? 2 : 1, maximumFractionDigits: signed ? 2 : 1 })} t`;

/**
 * Who holds gold (docs/todo/024). Display only: nothing in the app trades on
 * it.
 *
 * Managed money is the speculative money in COMEX gold futures. The COT index
 * puts its net position between its lowest (0) and highest (100) of the last
 * three years; near 100 the crowd is already long, which is when sharp
 * flushes happen. Positions are as of Tuesday and published on Friday.
 * GLD tonnes are the largest gold ETF's holdings: a rising figure is money
 * buying physical gold through the fund.
 */
export function PositioningSection() {
  const poll = usePositioning();
  const p = poll.data;
  if (!p) {
    return poll.error
      ? <p className="text-[11px] text-ink-3">Positioning unavailable: {poll.error.message}</p>
      : null;
  }
  const cot = p.cot;
  const gld = p.gld;
  const idx = cot?.cot_index;

  return (
    <section data-testid="positioning" className="space-y-2">
      <h3 className="text-xs font-semibold text-ink-2">Gold positioning</h3>
      <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <StatCard label="Managed money net" value={whole(cot?.mm_net)}
          hint={cot ? `Long ${whole(cot.mm_long)}, short ${whole(cot.mm_short)} contracts (100 oz each). ${cot.net_pct_oi ?? "—"}% of open interest.` : undefined} />
        <StatCard label="Change on the week" value={whole(cot?.net_change, true)}
          valueClassName={cn(cot?.net_change != null && (cot.net_change >= 0 ? "text-profit" : "text-loss"))} />
        <StatCard label="COT index (3 years)" value={idx == null ? "—" : idx.toFixed(0)}
          valueClassName={cn(idx != null && idx >= 80 && "text-loss", idx != null && idx <= 20 && "text-profit")}
          hint="Where this week's net sits between its 3-year low (0) and high (100). Above 80: crowded long. Below 20: washed out." />
        <StatCard label="GLD holdings" value={tonnes(gld?.tonnes)} />
        <StatCard label="GLD 5 days" value={tonnes(gld?.change_5d, true)}
          valueClassName={cn(gld?.change_5d != null && (gld.change_5d >= 0 ? "text-profit" : "text-loss"))} />
        <StatCard label="GLD 20 days" value={tonnes(gld?.change_20d, true)}
          valueClassName={cn(gld?.change_20d != null && (gld.change_20d >= 0 ? "text-profit" : "text-loss"))} />
      </div>
      <p className="text-[11px] text-ink-3">
        CFTC report of {cot?.report_date ?? "—"} (positions as of that Tuesday)
        {p.cot_error && <span className="text-loss"> · CFTC: {p.cot_error}</span>}
        {" · "}GLD as of {gld?.date ?? "—"}
        {p.gld_error && <span className="text-loss"> · GLD: {p.gld_error}</span>}
        . For context only; nothing trades on these.
      </p>
    </section>
  );
}
