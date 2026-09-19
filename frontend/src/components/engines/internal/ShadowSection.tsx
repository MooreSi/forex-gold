import { EmptyState } from "@/components/shared/EmptyState";
import { formatBrokerTime, formatMoney, pnlColour } from "@/components/shared/format";
import { asArray } from "@/lib/asArray";
import { cn } from "@/lib/cn";

export interface ShadowRow {
  variant: string;
  is_champion: boolean;
  n_taken: number;
  n_skipped: number;
  net: number;
  mean_r: number | null;
}

export interface HistoryRow {
  ts: number;
  signal_ref: string;
  variant: string;
  would_take: number;
  reason: string | null;
  direction: string | null;
  status: string | null;
  outcome: string | null;
  net: number | null;
  r: number | null;
}

/**
 * The virtual trades: what each configuration would have done.
 *
 * The champion is what is live. A challenger is a configuration being measured
 * against it on the same signals, without any money — which is the only honest
 * way to find out whether a change is an improvement before shipping it.
 *
 * **A skip is a row, not an absence.** A variant that skipped a losing trade
 * and one that never saw it both contribute nothing to the P&L, and only one
 * of them is evidence. The skipped rows carry the outcome they avoided.
 *
 * `mean_r` and `r` are blank, never 0.000, where there is nothing to score.
 * Zero expectancy and no evidence are different statements, and a table that
 * renders both as 0.000 invites the wrong one to be acted on — the rule
 * `shadow.report()` states in its own docstring.
 */
export function ShadowSection({ shadow, history, realised }: {
  shadow: unknown; history: unknown; realised: Record<string, unknown>;
}) {
  const rows = asArray<ShadowRow>(shadow);
  const decisions = asArray<HistoryRow>(history);
  // `{n, total, per_trade}` -- the shape `panel_data.get_realised_pnl` really
  // returns. The first version of this read `net_pnl`, a key that does not
  // exist, so the line was silently absent: the same mistake this panel's own
  // pro-model section had been making, made again two hundred lines away.
  // Checked against the live payload, 2026-09-19.
  const net = typeof realised?.["total"] === "number"
    ? (realised["total"] as number) : null;
  const n = typeof realised?.["n"] === "number" ? (realised["n"] as number) : null;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className="text-xs font-semibold text-ink-1">Virtual trades</h3>
        {net != null && (
          <span data-testid="shadow-realised" className="text-[11px] text-ink-3">
            the engine's real closed P&amp;L is{" "}
            <span className={cn("num font-semibold", pnlColour(net))}>
              {formatMoney(net)}
            </span>
            {/* -$206 over 58 trades and -$206 over 3 are different
                statements, and only one of them is a verdict. */}
            {n != null && <span className="num"> over {n}</span>}
          </span>
        )}
      </div>

      {rows.length === 0 ? (
        <EmptyState
          title="No variant has seen a closed signal yet"
          hint="A challenger is scored only on signals that have since closed."
        />
      ) : (
        <table data-testid="shadow-table" className="w-full text-left text-[11px]">
          <thead className="text-ink-3">
            <tr>
              {["Variant", "Taken", "Skipped", "Net", "Mean R"].map((h) => (
                <th key={h} className="px-2 py-1 font-normal">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="num">
            {rows.map((r) => (
              <tr key={r.variant} data-testid={`variant-${r.variant}`}
                className="border-t border-line">
                <td className="px-2 py-1 text-ink-1">
                  {r.variant}
                  {r.is_champion && (
                    <span className="ml-1.5 rounded bg-accent/15 px-1 py-0.5 text-[9px] text-accent">
                      live
                    </span>
                  )}
                </td>
                <td className="px-2 py-1 text-ink-2">{r.n_taken}</td>
                <td className="px-2 py-1 text-ink-3">{r.n_skipped}</td>
                <td className={cn("px-2 py-1", pnlColour(r.net))}>{formatMoney(r.net)}</td>
                <td className="px-2 py-1 text-ink-2">
                  {r.mean_r == null ? "—" : r.mean_r.toFixed(3)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div>
        <h4 className="text-xs font-semibold text-ink-1">Decision history</h4>
        <p className="mb-1 text-[10px] text-ink-3">
          Every variant's call on every signal, newest first. A skip on a loser
          is the variant being right.
        </p>
        {decisions.length === 0 ? (
          <EmptyState title="No decisions recorded yet" />
        ) : (
          <div className="max-h-72 overflow-auto">
            <table data-testid="shadow-history" className="w-full text-left text-[11px]">
              <thead className="text-ink-3">
                <tr>
                  {["When", "Variant", "Call", "Signal", "R", "Would have made", "Why"]
                    .map((h) => <th key={h} className="px-2 py-1 font-normal">{h}</th>)}
                </tr>
              </thead>
              <tbody className="num">
                {decisions.map((d, i) => {
                  const took = Boolean(d.would_take);
                  return (
                    <tr key={`${d.signal_ref}-${d.variant}-${i}`}
                      data-testid={`decision-${d.signal_ref}-${d.variant}`}
                      className="border-t border-line">
                      <td className="px-2 py-1 text-ink-3">{formatBrokerTime(d.ts)}</td>
                      <td className="px-2 py-1 text-ink-2">{d.variant}</td>
                      <td className={cn("px-2 py-1", took ? "text-profit" : "text-ink-3")}>
                        {took ? "took" : "skipped"}
                      </td>
                      <td className="px-2 py-1 text-ink-3">
                        {d.direction ?? "—"} · {d.status ?? "—"}
                      </td>
                      <td className="px-2 py-1 text-ink-2">
                        {d.r == null ? "—" : d.r.toFixed(2)}
                      </td>
                      <td className={cn("px-2 py-1",
                        d.net == null ? "text-ink-3" : pnlColour(took ? d.net : -d.net))}>
                        {d.net == null ? "—" : formatMoney(d.net)}
                      </td>
                      <td className="px-2 py-1 text-ink-3">{d.reason || "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
