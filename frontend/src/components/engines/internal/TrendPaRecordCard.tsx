import { formatMoney, formatPercent } from "@/components/shared/format";
import { asArray, asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";

/**
 * One Trend PA record: the replay's, or the live one. Drawn twice, side by
 * side, from the same shape (`services/trend_pa/stats.summarize`), so the two
 * can be read against each other and never pooled.
 *
 * Every figure is in R, the risk each trade took. The win rate carries the
 * break-even line beside it: at 1:2 a no-edge entry wins a third of the time,
 * so "35%" means nothing until it is read against "33.3%".
 */
interface TrendPaRecordCardProps {
  title: string;
  hint: string;
  summary: unknown;
  testId: string;
}

function num(value: unknown): number | null {
  if (value == null || value === "") return null;
  const v = typeof value === "number" ? value : Number(value);
  return Number.isFinite(v) ? v : null;
}

const signedR = (v: number | null, dp = 3) =>
  v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(dp)}R`;
const tone = (v: number | null) =>
  v == null ? "text-ink-3" : v > 0 ? "text-profit" : v < 0 ? "text-loss" : "text-ink-1";

function Figure({ label, value, hint, cls, testId }: {
  label: string; value: string; hint?: string; cls?: string; testId: string;
}) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wider text-ink-3">{label}</p>
      <p data-testid={testId} className={cn("num text-sm font-semibold", cls ?? "text-ink-1")}>
        {value}
        {hint && <span className="block text-[10px] font-normal text-ink-3">{hint}</span>}
      </p>
    </div>
  );
}

/** The compounded $1,000 paper balance, trade by trade. Not time: trades are
 *  not evenly spaced, and the x axis is their order. */
function Curve({ points }: { points: Record<string, unknown>[] }) {
  const ys = points.map((p) => num(p["balance"]) ?? 0);
  if (ys.length < 2) return null;
  const lo = Math.min(...ys, 1000);
  const hi = Math.max(...ys, 1000);
  const span = hi - lo || 1;
  const x = (i: number) => (i / (ys.length - 1)) * 300;
  const y = (v: number) => 40 - ((v - lo) / span) * 40;
  const d = ys.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <svg viewBox="0 0 300 40" className="h-10 w-full" aria-label="paper balance, trade by trade">
      <line x1="0" x2="300" y1={y(1000)} y2={y(1000)} className="stroke-line" strokeDasharray="3 3" />
      <path d={d} fill="none" strokeWidth="1.5"
        className={ys[ys.length - 1] >= 1000 ? "stroke-profit" : "stroke-loss"} />
    </svg>
  );
}

function Split({ title, rows, testId }: { title: string; rows: Record<string, unknown>[]; testId: string }) {
  return (
    <div>
      <p className="mb-0.5 text-[10px] font-semibold text-ink-2">{title}</p>
      {rows.length === 0 ? <p className="text-[10px] text-ink-3">nothing closed yet</p> : (
        <table data-testid={testId} className="w-full text-[10px]">
          <tbody className="num">
            {rows.map((r) => (
              <tr key={String(r["key"])} className="border-t border-line">
                <td className="py-0.5 text-ink-1">{String(r["key"])}</td>
                <td className="py-0.5 text-right text-ink-3">{num(r["n"])}</td>
                <td className="py-0.5 text-right text-ink-2">{formatPercent((num(r["win_rate"]) ?? 0) * 100, 0)}</td>
                <td className={cn("py-0.5 text-right", tone(num(r["avg_r"])))}>{signedR(num(r["avg_r"]), 2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function TrendPaRecordCard({ title, hint, summary, testId }: TrendPaRecordCardProps) {
  const s = asObject(summary);
  const n = num(s["n"]) ?? 0;
  const wr = num(s["win_rate"]);
  const be = num(s["breakeven_win_rate"]);
  const pf = num(s["profit_factor"]);
  const avg = num(s["avg_r"]);
  const balance = num(s["balance"]);
  return (
    <section data-testid={testId} className="space-y-2 rounded border border-line p-3">
      <div>
        <h4 className="text-xs font-semibold text-ink-1">{title}</h4>
        <p className="text-[10px] text-ink-3">{hint}</p>
      </div>
      <div className="grid grid-cols-3 gap-2">
        <Figure label="Trades" testId="tpa-n" value={String(n)}
          hint={n ? `${num(s["wins"])} won, ${num(s["losses"])} lost` : undefined} />
        <Figure label="Win rate" testId="tpa-win-rate"
          value={wr == null ? "—" : formatPercent(wr * 100)}
          hint={be == null ? undefined : `break-even ${formatPercent(be * 100)}`}
          cls={wr == null || be == null ? undefined : wr > be ? "text-profit" : "text-loss"} />
        <Figure label="Avg per trade" testId="tpa-avg-r" value={signedR(avg)} cls={tone(avg)}
          hint={n ? `total ${signedR(num(s["total_r"]), 1)}` : undefined} />
        {/* Null, not 0 and not infinity: a record with no losses has no ratio
            yet, and 0.00 reads as the worst possible one. */}
        <Figure label="Profit factor" testId="tpa-pf"
          value={pf == null ? "not yet" : pf.toFixed(2)}
          cls={pf == null ? "text-ink-3" : pf >= 1 ? "text-profit" : "text-loss"} />
        <Figure label="Worst drawdown" testId="tpa-dd"
          value={n ? `${(num(s["max_drawdown_r"]) ?? 0).toFixed(1)}R` : "—"}
          hint={n ? `${formatPercent(num(s["max_drawdown_pct"]) ?? 0)} of the paper balance` : undefined} />
        <Figure label="Paper $1,000" testId="tpa-balance"
          value={balance == null ? "—" : formatMoney(balance)} hint="1% risk a trade, compounded"
          cls={balance == null ? undefined : tone(balance - 1000)} />
      </div>
      <Curve points={asArray<Record<string, unknown>>(s["curve"])} />
      <div className="grid gap-2 sm:grid-cols-3">
        <Split title="Session" testId="tpa-split-session" rows={asArray(s["by_session"])} />
        <Split title="Pattern" testId="tpa-split-pattern" rows={asArray(s["by_pattern"])} />
        <Split title="Direction" testId="tpa-split-direction" rows={asArray(s["by_direction"])} />
      </div>
    </section>
  );
}
