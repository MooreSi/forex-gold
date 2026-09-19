import { useCallback } from "react";
import { api } from "@/api/client";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatBrokerTime, formatMoney, pnlColour } from "@/components/shared/format";
import { usePoll } from "@/hooks/usePoll";
import { asArray } from "@/lib/asArray";
import { cn } from "@/lib/cn";

/**
 * DPM: the Dynamic Position Management calibration, and what it produced.
 *
 * The sixth NiceGUI Analysis sub-tab, and the last one the React port was
 * missing. `/api/ai/dpm` has served all three tables since the port; nothing
 * rendered them.
 *
 * DPM decides where breakeven and the trail go for a given session and
 * momentum, and it CALIBRATES those multipliers from what previous trades
 * actually did. So the three tables answer three different questions and are
 * kept apart deliberately:
 *
 *   * **Runs** — has the calibration been run, and on how much evidence. A
 *     calibration on forty trades is a different object from one on four.
 *   * **Buckets** — the multipliers currently in force, per session and
 *     momentum bucket, each beside the sample it was derived from.
 *   * **Trades** — what each managed trade did, with the parameters that were
 *     in effect when it opened. This is where a bad bucket shows up.
 *
 * Every table can legitimately be empty on a fresh install, and each says so
 * in its own words rather than the panel showing one blank space.
 */
interface DpmState {
  performance: Record<string, unknown>[];
  calibration: Record<string, unknown>[];
  runs: Record<string, unknown>[];
}

function num(value: unknown): number | null {
  if (value == null || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

/** A measurement, or an em dash. Never 0.00 for "not measured". */
function fixed(value: unknown, dp = 2, suffix = ""): string {
  const n = num(value);
  return n == null ? "—" : `${n.toFixed(dp)}${suffix}`;
}

function Table({ head, children, testId }: {
  head: string[]; children: React.ReactNode; testId: string;
}) {
  return (
    <table data-testid={testId} className="w-full text-left text-[11px]">
      <thead className="text-ink-3">
        <tr>{head.map((h) => <th key={h} className="px-2 py-1 font-normal">{h}</th>)}</tr>
      </thead>
      <tbody className="num">{children}</tbody>
    </table>
  );
}

export function DpmSection() {
  const poll = usePoll<DpmState>(
    "ai/dpm",
    useCallback(() => api.get<DpmState>("/api/ai/dpm"), []),
    60_000,
  );

  if (!poll.data) {
    return <EmptyState title={poll.error ? "Could not load the DPM tables" : "Loading"}
      hint={poll.error?.message} />;
  }

  const runs = asArray<Record<string, unknown>>(poll.data.runs);
  const buckets = asArray<Record<string, unknown>>(poll.data.calibration);
  const trades = asArray<Record<string, unknown>>(poll.data.performance);

  return (
    <div className="space-y-4">
      <p className="text-[11px] text-ink-3">
        Dynamic Position Management decides where breakeven and the trail sit
        for a given session and momentum, and calibrates those multipliers from
        what previous trades actually did.
      </p>

      <section>
        <h3 className="mb-1 text-xs font-semibold text-ink-1">Calibration runs</h3>
        {runs.length === 0 ? (
          <EmptyState title="DPM has never been calibrated on this install" />
        ) : (
          <Table testId="dpm-runs"
            head={["When", "Buckets", "Samples", "Win rate", "Avg R", "Profit factor"]}>
            {runs.map((r, i) => (
              <tr key={String(r["calibrated_at"] ?? i)} className="border-t border-line">
                <td className="px-2 py-1 text-ink-3">
                  {formatBrokerTime(num(r["calibrated_at"]))}
                </td>
                <td className="px-2 py-1 text-ink-2">{String(r["buckets"] ?? "—")}</td>
                {/* Beside every average, because a mean over four trades is
                    not a measurement. */}
                <td className="px-2 py-1 text-ink-2">{String(r["total_samples"] ?? "—")}</td>
                <td className="px-2 py-1 text-ink-2">{fixed(r["avg_win_rate"], 1, "%")}</td>
                <td className="px-2 py-1 text-ink-2">{fixed(r["avg_r"])}</td>
                <td className="px-2 py-1 text-ink-2">{fixed(r["avg_pf"])}</td>
              </tr>
            ))}
          </Table>
        )}
      </section>

      <section>
        <h3 className="mb-1 text-xs font-semibold text-ink-1">
          Multipliers in force
        </h3>
        {buckets.length === 0 ? (
          <EmptyState title="No calibrated buckets yet"
            hint="DPM falls back to its built-in defaults until there are." />
        ) : (
          <Table testId="dpm-buckets"
            head={["Session", "Momentum", "Breakeven", "Trail", "TP1 partial",
                   "Samples", "Win rate", "Avg R"]}>
            {buckets.map((b, i) => (
              <tr key={i} data-testid={`dpm-bucket-${b["session"]}-${b["momentum_bucket"]}`}
                className="border-t border-line">
                <td className="px-2 py-1 text-ink-1">{String(b["session"] ?? "—")}</td>
                <td className="px-2 py-1 text-ink-2">{String(b["momentum_bucket"] ?? "—")}</td>
                <td className="px-2 py-1 text-ink-2">{fixed(b["be_multiplier"], 2, "x")}</td>
                <td className="px-2 py-1 text-ink-2">{fixed(b["trail_multiplier"], 2, "x")}</td>
                <td className="px-2 py-1 text-ink-2">{fixed(b["tp1_partial_pct"], 0, "%")}</td>
                <td className={cn("px-2 py-1",
                  (num(b["sample_size"]) ?? 0) < 20 ? "text-warning" : "text-ink-2")}>
                  {String(b["sample_size"] ?? "—")}
                </td>
                <td className="px-2 py-1 text-ink-2">{fixed(b["win_rate"], 1, "%")}</td>
                <td className="px-2 py-1 text-ink-2">{fixed(b["avg_r_multiple"])}</td>
              </tr>
            ))}
          </Table>
        )}
      </section>

      <section>
        <h3 className="mb-1 text-xs font-semibold text-ink-1">Managed trades</h3>
        {trades.length === 0 ? (
          <EmptyState title="DPM has not managed a closed trade yet" />
        ) : (
          <div className="max-h-80 overflow-auto">
            <Table testId="dpm-trades"
              head={["Closed", "Side", "Exit", "P&L", "R", "Held", "Session",
                     "Momentum", "BE used", "Trail used"]}>
              {trades.map((t, i) => (
                <tr key={String(t["trade_id"] ?? i)}
                  data-testid={`dpm-trade-${t["trade_id"] ?? i}`}
                  className="border-t border-line">
                  <td className="px-2 py-1 text-ink-3">
                    {formatBrokerTime(num(t["close_time"]))}
                  </td>
                  <td className={cn("px-2 py-1",
                    t["direction"] === "BUY" ? "text-profit" : "text-loss")}>
                    {String(t["direction"] ?? "—")}
                  </td>
                  <td className="px-2 py-1 text-ink-3">{String(t["exit_type"] ?? "—")}</td>
                  <td className={cn("px-2 py-1", pnlColour(num(t["final_pnl"]) ?? 0))}>
                    {num(t["final_pnl"]) == null ? "—" : formatMoney(num(t["final_pnl"]))}
                  </td>
                  <td className="px-2 py-1 text-ink-2">{fixed(t["r_multiple"])}</td>
                  <td className="px-2 py-1 text-ink-3">{fixed(t["hold_minutes"], 0, "m")}</td>
                  <td className="px-2 py-1 text-ink-3">{String(t["session_at_entry"] ?? "—")}</td>
                  <td className="px-2 py-1 text-ink-3">{String(t["momentum_label"] ?? "—")}</td>
                  {/* The parameters that were in effect WHEN IT OPENED, not
                      the ones in force now. A trade managed under an old
                      calibration is the evidence for changing it. */}
                  <td className="px-2 py-1 text-ink-3">{fixed(t["be_multiplier_used"], 2, "x")}</td>
                  <td className="px-2 py-1 text-ink-3">{fixed(t["trail_multiplier_used"], 2, "x")}</td>
                </tr>
              ))}
            </Table>
          </div>
        )}
      </section>
    </div>
  );
}
