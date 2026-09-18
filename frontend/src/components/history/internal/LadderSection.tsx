import { EmptyState } from "@/components/shared/EmptyState";

/**
 * How far up its TP ladder each strategy actually gets.
 *
 * `n` is shown beside every reach because a mean over four trades is not a
 * measurement, and a strategy that looks best on this table is usually the one
 * with the fewest samples.
 */
export function LadderSection({ ladder }: { ladder: Record<string, Record<string, unknown>> }) {
  const rows = Object.entries(ladder ?? {});
  if (rows.length === 0) {
    return <EmptyState title="Not enough closed trades to measure ladder reach" />;
  }
  return (
    <table className="w-full max-w-md text-xs">
      <thead>
        <tr className="text-left text-[10px] uppercase tracking-wide text-ink-3">
          <th className="py-1 font-medium">Strategy</th>
          <th className="py-1 font-medium">Average reach</th>
          <th className="py-1 font-medium">Trades</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([strategy, row]) => (
          <tr key={strategy} className="border-t border-line">
            <td className="py-1.5 text-ink-1">{strategy}</td>
            <td className="num py-1.5 text-ink-2">
              {typeof row["reach"] === "number" ? `TP ${row["reach"].toFixed(1)}` : "—"}
            </td>
            <td className="num py-1.5 text-ink-3">{String(row["n"] ?? "—")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
