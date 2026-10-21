import { EmptyState } from "@/components/shared/EmptyState";

/**
 * How far up its TP ladder each strategy actually gets.
 *
 * `n` is shown beside every reach because a mean over four trades is not a
 * measurement, and a strategy that looks best on this table is usually the one
 * with the fewest samples.
 *
 * The fields are what `get_strategy_ladder_reach` sends. Until 2026-10-21 this
 * read a `reach` field nothing sent, and every row was a dash.
 */
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

const COLUMNS: { key: string; label: string; hint: string }[] = [
  { key: "no_tp", label: "No TP", hint: "Closed without reaching TP1" },
  { key: "tp1_2", label: "TP1-2", hint: "Deepest rung was TP1 or TP2" },
  { key: "tp3_4", label: "TP3-4", hint: "Deepest rung was TP3 or TP4" },
  { key: "tp5_plus", label: "TP5+", hint: "Reached TP5 or beyond" },
  { key: "stopped_after_tp", label: "Banked, then stopped",
    hint: "Banked at least one rung, then exited on a stop: a win that was cut short" },
];

export function LadderSection({ ladder }: { ladder: Record<string, Record<string, unknown>> }) {
  const rows = Object.entries(ladder ?? {})
    .sort(([, a], [, b]) => (num(b["n"]) ?? 0) - (num(a["n"]) ?? 0));
  if (rows.length === 0) {
    return <EmptyState title="Not enough closed trades to measure ladder reach" />;
  }
  return (
    <div className="space-y-2">
      <p className="text-[11px] text-ink-3">
        How far up its TP ladder each strategy's closed trades got, over the window. Average reach
        is the mean rung, counting trades that never reached TP1 as 0.
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-[10px] uppercase tracking-wide text-ink-3">
              <th className="py-1 pr-3 font-medium">Strategy</th>
              <th className="py-1 pr-3 font-medium">Trades</th>
              <th className="py-1 pr-3 font-medium">Average reach</th>
              {COLUMNS.map((c) => (
                <th key={c.key} title={c.hint} className="py-1 pr-3 font-medium">{c.label}</th>
              ))}
              <th className="py-1 pr-3 font-medium">Deepest</th>
              <th className="py-1 font-medium">Win rate</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([strategy, row]) => {
              const avg = num(row["avg_rung"]);
              const top = num(row["top_rung"]);
              const wr = num(row["win_rate"]);
              return (
                <tr key={strategy} className="border-t border-line">
                  <td className="py-1.5 pr-3 text-ink-1">{strategy.replace(/^template:/, "")}</td>
                  <td className="num py-1.5 pr-3 text-ink-3">{String(num(row["n"]) ?? "—")}</td>
                  <td className="num py-1.5 pr-3 text-ink-1">{avg != null ? `TP ${avg.toFixed(2)}` : "—"}</td>
                  {COLUMNS.map((c) => (
                    <td key={c.key} className="num py-1.5 pr-3 text-ink-2">
                      {String(num(row[c.key]) ?? "—")}
                    </td>
                  ))}
                  <td className="num py-1.5 pr-3 text-ink-2">{top ? `TP${top}` : "—"}</td>
                  <td className="num py-1.5 text-ink-2">{wr != null ? `${wr.toFixed(1)}%` : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
