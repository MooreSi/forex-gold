import { EmptyState } from "@/components/shared/EmptyState";
import { formatPercent, formatSignedMoney, pnlColour } from "@/components/shared/format";

/**
 * The channel table: the measured numbers, before anybody pays for an opinion
 * about them.
 *
 * Every column here answers a question the prompt asks the model. Phantom TPs
 * are channel claims that a TP was hit on a trade that stopped out. The
 * 50%-at-TP1 column is what the same signals would have produced under a
 * partial-close rule, which is the single most actionable line in the report
 * — a channel whose simulated figure is far better than its actual one is not
 * a bad channel, it is a badly managed one.
 *
 * The other two subjects have their own tables; this file is no longer a
 * dumping ground for raw JSON.
 */
export function EvidenceSection({ subject, evidence }: { subject: string; evidence: unknown }) {
  if (evidence == null) {
    return <EmptyState title="Loading the evidence" />;
  }
  if (subject === "channels" && Array.isArray(evidence)) {
    if (evidence.length === 0) {
      return <EmptyState title="No channel has produced a signal in this window" />;
    }
    return (
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wide text-ink-3">
            <th className="py-1 font-medium">Channel</th>
            <th className="py-1 font-medium">Signals</th>
            <th className="py-1 font-medium">Closed</th>
            <th className="py-1 font-medium">Win rate</th>
            <th className="py-1 font-medium">P&amp;L</th>
            <th className="py-1 font-medium" title="Claimed a TP on a trade that stopped out">
              Phantom TPs
            </th>
            <th className="py-1 font-medium"
              title="What the same signals would have produced closing 50% at TP1 and moving the stop to breakeven">
              At 50% / TP1
            </th>
            <th className="py-1 font-medium" title="Longest run of losing trades">
              Worst run
            </th>
          </tr>
        </thead>
        <tbody>
          {(evidence as Record<string, never>[]).map((row, i) => {
            const stats = (row["stats"] ?? {}) as Record<string, unknown>;
            const pnl = typeof stats["total_pnl"] === "number" ? stats["total_pnl"] : null;
            const phantoms = Number(stats["phantom_tp_count"] ?? 0);
            const simulated = typeof stats["simulated_50pct_pnl_sum"] === "number"
              ? stats["simulated_50pct_pnl_sum"] : null;
            return (
              <tr key={String(row["channel_name"] ?? i)} className="border-t border-line">
                <td className="py-1.5 text-ink-1">{String(row["channel_name"] ?? "—")}</td>
                <td className="num py-1.5 text-ink-2">{String(stats["total_signals"] ?? "—")}</td>
                <td className="num py-1.5 text-ink-2">{String(stats["closed_trades"] ?? "—")}</td>
                <td className="num py-1.5 text-ink-2">
                  {formatPercent(
                    typeof stats["win_rate_pct"] === "number" ? stats["win_rate_pct"] : null,
                  )}
                </td>
                <td className={`num py-1.5 ${pnlColour(pnl)}`}>{formatSignedMoney(pnl)}</td>
                <td className={`num py-1.5 ${phantoms > 0 ? "text-warning" : "text-ink-3"}`}>
                  {phantoms}
                </td>
                <td className={`num py-1.5 ${pnlColour(simulated)}`}>
                  {simulated == null ? "—" : formatSignedMoney(simulated)}
                </td>
                <td className="num py-1.5 text-ink-3">
                  {String(stats["max_consecutive_losses"] ?? "—")}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    );
  }
  // No raw-JSON fallback any more: every subject has its own table, and a
  // subject reaching here would be a wiring mistake worth seeing as one.
  return <EmptyState title="No evidence for this subject" />;
}
