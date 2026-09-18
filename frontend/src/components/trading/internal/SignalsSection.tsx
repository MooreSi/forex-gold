import { EmptyState } from "@/components/shared/EmptyState";
import { formatPrice } from "@/components/shared/format";

/**
 * Signals the engines and the Telegram reader have produced.
 *
 * Read-only here on purpose: acting on a signal opens a position, and that
 * control belongs with the rest of the money path rather than on a list row
 * where it is one mis-click from a trade.
 */
export function SignalsSection({ signals }: { signals: Record<string, unknown>[] }) {
  if (signals.length === 0) {
    return (
      <EmptyState
        title="No signals yet"
        hint="Engine and Telegram signals appear here as they are produced."
      />
    );
  }
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-left text-[10px] uppercase tracking-wide text-ink-3">
          <th className="py-1 font-medium">Source</th>
          <th className="py-1 font-medium">Side</th>
          <th className="py-1 font-medium">Entry</th>
          <th className="py-1 font-medium">Status</th>
        </tr>
      </thead>
      <tbody>
        {signals.map((s, i) => (
          <tr key={String(s["id"] ?? i)} className="border-t border-line">
            <td className="py-1.5 text-ink-2">{String(s["source"] ?? s["channel"] ?? "—")}</td>
            <td className={s["direction"] === "SELL" ? "py-1.5 text-loss" : "py-1.5 text-profit"}>
              {String(s["direction"] ?? "—")}
            </td>
            <td className="num py-1.5 text-ink-1">
              {formatPrice(typeof s["entry"] === "number" ? (s["entry"] as number) : null)}
            </td>
            <td className="py-1.5 text-ink-3">{String(s["status"] ?? "—")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
