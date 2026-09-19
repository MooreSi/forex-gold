import { useCallback } from "react";
import { api } from "@/api/client";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatMoney, formatPercent, pnlColour } from "@/components/shared/format";
import { usePoll } from "@/hooks/usePoll";
import { asArray, asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";

/**
 * The Breakout engine's own panel.
 *
 * `panel_data.py` has declared these reads since the restructure and nothing
 * called any of them: the Signal Generator tab showed the Reversal engine's ML
 * gate and virtual trades in detail and said nothing about Breakout beyond a
 * Start/Stop card.
 *
 * The four splits are the point. An aggregate win rate hides a time window
 * that only loses — this engine's 12:00-15:00 UTC losses hid in its own
 * headline figure for weeks, which is why the session split exists at all.
 *
 * Every number carries the count it was measured over, because a 100% win rate
 * over two trades is not a measurement.
 */
interface BreakoutReport {
  stats: Record<string, unknown>;
  virtual_balance: number | null;
  max_drawdown: number | null;
  ml: {
    summary: Record<string, unknown>;
    metrics: Record<string, unknown>;
    thresholds: Record<string, unknown>;
  };
  by_session: Record<string, unknown>[];
  by_adx: Record<string, unknown>[];
  by_type: Record<string, unknown>[];
  by_bias: Record<string, unknown>[];
}

function num(value: unknown): number | null {
  if (value == null || value === "") return null;
  const v = typeof value === "number" ? value : Number(value);
  return Number.isFinite(v) ? v : null;
}

function Figure({ label, value, hint, tone, testId }: {
  label: string; value: string; hint?: string; tone?: string; testId: string;
}) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wider text-ink-3">{label}</p>
      <p data-testid={testId} className={cn("num text-sm font-semibold", tone ?? "text-ink-1")}>
        {value}
      </p>
      {hint && <p className="text-[10px] text-ink-3">{hint}</p>}
    </div>
  );
}

/** One performance split. The label column's key differs per split, so it is
 *  named by the caller rather than guessed. */
function Split({ title, rows, keyField, testId }: {
  title: string; rows: Record<string, unknown>[]; keyField: string; testId: string;
}) {
  if (rows.length === 0) {
    return (
      <div>
        <p className="mb-1 text-[11px] font-semibold text-ink-2">{title}</p>
        <p className="text-[11px] text-ink-3">nothing closed yet</p>
      </div>
    );
  }
  return (
    <div>
      <p className="mb-1 text-[11px] font-semibold text-ink-2">{title}</p>
      <table data-testid={testId} className="w-full text-left text-[11px]">
        <tbody className="num">
          {rows.map((r, i) => {
            const pnl = num(r["net_pnl"] ?? r["pnl"]);
            const n = num(r["n"] ?? r["count"]);
            return (
              <tr key={String(r[keyField] ?? i)} className="border-t border-line">
                <td className="py-1 text-ink-1">{String(r[keyField] ?? "—")}</td>
                {/* The count sits beside every figure: a 100% win rate over
                    two trades is not a measurement. */}
                <td className="py-1 text-right text-ink-3">{n ?? "—"}</td>
                <td className={cn("py-1 text-right", pnlColour(pnl ?? 0))}>
                  {pnl == null ? "—" : formatMoney(pnl)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function BreakoutSection() {
  const poll = usePoll<BreakoutReport>(
    "engines/breakout/report",
    useCallback(() => api.get<BreakoutReport>("/api/engines/breakout/report"), []),
    30_000,
  );

  if (!poll.data) {
    return <EmptyState title={poll.error ? "Could not load the Breakout panel" : "Loading"}
      hint={poll.error?.message} />;
  }

  const stats = asObject(poll.data.stats);
  const ml = asObject(poll.data.ml);
  const summary = asObject(ml["summary"]);
  const metrics = asObject(ml["metrics"]);
  const thresholds = asObject(ml["thresholds"]);

  const total = num(stats["total"]) ?? 0;
  const trained = summary["trained"] === true;
  const labelled = num(summary["labeled_count"]) ?? 0;
  const needed = num(thresholds["min_train_samples"]) ?? num(summary["min_needed"]);
  const balance = num(poll.data.virtual_balance);
  const drawdown = num(poll.data.max_drawdown);

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <Figure label="Closed" testId="bo-total" value={total ? String(total) : "—"} />
        <Figure label="Win rate" testId="bo-win-rate"
          value={num(stats["win_rate"]) == null ? "—" : formatPercent(num(stats["win_rate"]))}
          hint={total ? `of ${total}` : undefined} />
        <Figure label="Net P&L" testId="bo-pnl"
          value={num(stats["net_pnl"]) == null ? "—" : formatMoney(num(stats["net_pnl"]))}
          tone={pnlColour(num(stats["net_pnl"]) ?? 0)} />
        <Figure label="Paper balance" testId="bo-balance"
          value={balance == null ? "—" : formatMoney(balance)}
          hint="the engine's own, not the account" />
        <Figure label="Worst drawdown" testId="bo-drawdown"
          value={drawdown == null ? "—" : `${drawdown.toFixed(1)}%`}
          tone={drawdown ? "text-loss" : undefined} />
      </div>

      <div className="rounded border border-line bg-surface-2/40 p-2.5">
        <p className="flex flex-wrap items-baseline gap-2 text-[11px]">
          <span className="font-semibold text-ink-1">Breakout classifier</span>
          <span data-testid="bo-ml-state"
            className={cn("rounded px-1.5 py-0.5 text-[10px] font-semibold",
              trained ? "bg-profit/15 text-profit" : "bg-warning/15 text-warning")}>
            {trained ? "in use" : "not trained"}
          </span>
          {/* "40 labelled" means nothing without the number it has to reach. */}
          <span className="num text-ink-3">
            {labelled} labelled{needed != null && ` of ${needed} needed`}
          </span>
          {num(metrics["brier_now"]) != null && (
            <span className="num text-ink-3"
              title="Brier score: lower is better calibrated">
              Brier {(num(metrics["brier_now"]) ?? 0).toFixed(3)}
            </span>
          )}
          {num(metrics["mcc_rolling"]) != null && (
            <span className="num text-ink-3"
              title="Rolling Matthews correlation: 0 is a coin, 1 is perfect">
              MCC {(num(metrics["mcc_rolling"]) ?? 0).toFixed(3)}
            </span>
          )}
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {/* The session split is the one that earned its place: this engine's
            12:00-15:00 UTC losses hid in the headline figure for weeks. */}
        <Split title="By session" rows={asArray(poll.data.by_session)}
          keyField="session" testId="bo-by-session" />
        <Split title="By ADX band" rows={asArray(poll.data.by_adx)}
          keyField="band" testId="bo-by-adx" />
        <Split title="By breakout type" rows={asArray(poll.data.by_type)}
          keyField="type" testId="bo-by-type" />
        <Split title="By HTF bias" rows={asArray(poll.data.by_bias)}
          keyField="bias" testId="bo-by-bias" />
      </div>
    </div>
  );
}
