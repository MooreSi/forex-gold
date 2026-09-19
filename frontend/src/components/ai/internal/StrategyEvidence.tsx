import { EmptyState } from "@/components/shared/EmptyState";
import { formatSignedMoney, pnlColour } from "@/components/shared/format";
import { asArray, asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";

/**
 * DPM against fixed exits, as the NiceGUI page put it: a head-to-head first,
 * then the fixed strategies broken out.
 *
 * `count` sits beside every average because a win rate over five trades is not
 * a measurement, and this table exists to decide whether to leave an adaptive
 * exit system switched on.
 */
interface Stats {
  count?: number; wins?: number; win_rate?: number; total_pnl?: number;
  avg_pnl?: number; profit_factor?: number; avg_hold_min?: number;
  sl_exits?: number; be_exits?: number;
}

function n(value: unknown): number | null {
  if (value == null || value === "") return null;
  const v = typeof value === "number" ? value : Number(value);
  return Number.isFinite(v) ? v : null;
}

function Cell({ value, dp = 2, suffix = "" }: { value: unknown; dp?: number; suffix?: string }) {
  const v = n(value);
  return <span className="num">{v == null ? "—" : `${v.toFixed(dp)}${suffix}`}</span>;
}

function HeadToHead({ label, stats, testId }: {
  label: string; stats: Stats; testId: string;
}) {
  const count = n(stats?.count) ?? 0;
  const pnl = n(stats?.total_pnl);
  return (
    <div data-testid={testId}
      className={cn("rounded-lg border p-3",
        count === 0 ? "border-line bg-surface-1 opacity-60" : "border-line bg-surface-1")}>
      <p className="text-xs font-semibold text-ink-1">{label}</p>
      {count === 0 ? (
        // Not a row of zeros: "no trades" and "traded and broke even" are
        // different answers, and the second is the one zeros read as.
        <p className="mt-1 text-[11px] text-ink-3">no trades in this window</p>
      ) : (
        <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5 text-[11px] sm:grid-cols-3">
          <span className="text-ink-3">Trades <span className="num text-ink-2">{count}</span></span>
          <span className="text-ink-3">Win rate <Cell value={stats?.win_rate} dp={1} suffix="%" /></span>
          <span className={cn("text-ink-3")}>
            P&amp;L <span className={cn("num", pnlColour(pnl ?? 0))}>{formatSignedMoney(pnl)}</span>
          </span>
          <span className="text-ink-3">Avg <Cell value={stats?.avg_pnl} /></span>
          <span className="text-ink-3">PF <Cell value={stats?.profit_factor} /></span>
          <span className="text-ink-3">Held <Cell value={stats?.avg_hold_min} dp={0} suffix="m" /></span>
          <span className="text-ink-3">SL exits <span className="num text-ink-2">{n(stats?.sl_exits) ?? 0}</span></span>
          <span className="text-ink-3">BE exits <span className="num text-ink-2">{n(stats?.be_exits) ?? 0}</span></span>
        </div>
      )}
    </div>
  );
}

export function StrategyEvidence({ evidence }: { evidence: unknown }) {
  const data = asObject(evidence);
  const rows = asArray<Record<string, unknown>>(data["strategy_breakdown"]);
  const detail = asObject(data["dpm_detail"]);
  const total = n(data["total_closed"]) ?? 0;

  if (total === 0 && rows.length === 0) {
    return <EmptyState title="No closed trades in this window to compare" />;
  }

  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-2">
        <HeadToHead label="DPM-managed" stats={asObject(data["dpm_stats"])}
          testId="dpm-stats" />
        <HeadToHead label="Fixed strategies" stats={asObject(data["fixed_stats"])}
          testId="fixed-stats" />
      </div>

      {rows.length > 0 && (
        <div className="overflow-auto">
          <table data-testid="strategy-breakdown" className="w-full text-left text-[11px]">
            <thead className="text-ink-3">
              <tr>
                {["Strategy", "Trades", "Win rate", "P&L", "Avg", "PF", "Held", "SL exits"]
                  .map((h) => <th key={h} className="px-2 py-1 font-normal">{h}</th>)}
              </tr>
            </thead>
            <tbody className="num">
              {rows.map((r, i) => (
                <tr key={String(r["strategy"] ?? i)} className="border-t border-line">
                  <td className="px-2 py-1 text-ink-1">{String(r["strategy"] ?? "—")}</td>
                  <td className="px-2 py-1 text-ink-2">{String(r["count"] ?? "—")}</td>
                  <td className="px-2 py-1 text-ink-2"><Cell value={r["win_rate"]} dp={1} suffix="%" /></td>
                  <td className={cn("px-2 py-1", pnlColour(n(r["total_pnl"]) ?? 0))}>
                    {formatSignedMoney(n(r["total_pnl"]))}
                  </td>
                  <td className="px-2 py-1 text-ink-2"><Cell value={r["avg_pnl"]} /></td>
                  <td className="px-2 py-1 text-ink-2"><Cell value={r["profit_factor"]} /></td>
                  <td className="px-2 py-1 text-ink-3"><Cell value={r["avg_hold_min"]} dp={0} suffix="m" /></td>
                  <td className="px-2 py-1 text-ink-3">{String(r["sl_exits"] ?? "—")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(n(detail["count"]) ?? 0) > 0 && (
        <p data-testid="dpm-detail" className="text-[11px] text-ink-3">
          DPM detail: <span className="num">{n(detail["count"])}</span> trades,
          avg R <span className="num">{(n(detail["avg_r_multiple"]) ?? 0).toFixed(2)}</span>
          {" · "}
          {/* Calibrated against uncalibrated is the question behind the whole
              table: an adaptive system judged on trades it had not yet tuned
              for is being judged on its worst period. */}
          <span className="num">{n(detail["calibrated_trades"]) ?? 0}</span> calibrated,
          {" "}<span className="num">{n(detail["uncalibrated_trades"]) ?? 0}</span> not
        </p>
      )}
    </div>
  );
}
