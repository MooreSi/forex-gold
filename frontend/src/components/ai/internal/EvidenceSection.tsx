import { EmptyState } from "@/components/shared/EmptyState";
import { formatPercent, formatSignedMoney, pnlColour } from "@/components/shared/format";

/**
 * The measured numbers, shown before anybody pays for an opinion about them.
 *
 * Channel evidence gets a real table because it is the subject people actually
 * read; the other two are shown as their raw shape, which is honest about the
 * fact that they were built to be fed to a model rather than rendered.
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
          </tr>
        </thead>
        <tbody>
          {(evidence as Record<string, never>[]).map((row, i) => {
            const stats = (row["stats"] ?? {}) as Record<string, unknown>;
            const pnl = typeof stats["total_pnl"] === "number" ? stats["total_pnl"] : null;
            const phantoms = Number(stats["phantom_tp_count"] ?? 0);
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
              </tr>
            );
          })}
        </tbody>
      </table>
    );
  }
  return (
    <pre className="num max-h-96 overflow-auto rounded border border-line bg-surface-1 p-3 text-[11px] text-ink-2">
      {JSON.stringify(evidence, null, 2)}
    </pre>
  );
}
