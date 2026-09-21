import { EmptyState } from "@/components/shared/EmptyState";
import { formatPercent, formatSignedMoney, pnlColour } from "@/components/shared/format";
import { Tooltip } from "@/components/shared/Tooltip";

interface ChannelsScorecardProps {
  channels: Record<string, unknown>[];
  onPause: (source: string, paused: boolean) => Promise<void>;
}

function num(row: Record<string, unknown>, key: string): number | null {
  const raw = row[key];
  return typeof raw === "number" ? raw : null;
}

/**
 * Per-channel performance, and the switch that stops taking a channel's
 * signals.
 *
 * Pausing is not a money action in itself — it changes what the engines accept
 * next time and touches no open position — so it is a plain switch rather than
 * a confirmed one.
 */
export function ChannelsScorecard({ channels, onPause }: ChannelsScorecardProps) {
  if (channels.length === 0) {
    return <EmptyState title="No channel has closed a trade in this window" />;
  }
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-left text-[10px] uppercase tracking-wide text-ink-3">
          <th className="py-1 font-medium">Channel</th>
          <th className="py-1 font-medium">Trades</th>
          <th className="py-1 font-medium">Win rate</th>
          <th className="py-1 font-medium">Net P&amp;L</th>
          <th className="py-1 font-medium">Taking signals</th>
        </tr>
      </thead>
      <tbody>
        {channels.map((row, i) => {
          const source = String(row["source"] ?? i);
          const pnl = num(row, "net_pnl");
          const paused = row["paused"] === true;
          return (
            <tr key={source} className="border-t border-line">
              <td className="py-1.5 text-ink-1">{source}</td>
              <td className="num py-1.5 text-ink-2">{num(row, "trades") ?? "—"}</td>
              <td className="num py-1.5 text-ink-2">{formatPercent(num(row, "win_rate"))}</td>
              <td className={`num py-1.5 ${pnlColour(pnl)}`}>{formatSignedMoney(pnl)}</td>
              <td className="py-1.5">
                <Tooltip label={`Whether ${source}'s signals may open a position. Unticked, its messages are still read and still scored here — so you can keep watching a channel's record while it is not costing you anything.`}>
                  <input
                    type="checkbox"
                    aria-label={`Take signals from ${source}`}
                    checked={!paused}
                    onChange={(e) => void onPause(source, !e.target.checked)}
                    className="accent-accent"
                  />
                </Tooltip>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
