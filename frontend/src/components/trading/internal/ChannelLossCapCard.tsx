import { Tooltip } from "@/components/shared/Tooltip";
import { formatMoney, formatSignedMoney } from "@/components/shared/format";
import { asArray, asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";

export interface ChannelLossCapState {
  default_cap: number;
  overrides: Record<string, number>;
  channels: { channel: string; cap: number; day_pnl: number; held: boolean }[];
  error: string;
}

interface ChannelLossCapCardProps {
  /** `null` means the backend could not read it; `undefined`, not loaded yet. */
  cap: ChannelLossCapState | null | undefined;
  onSave: (defaultCap: number, overrides: Record<string, number>) => void;
}

function parseCap(text: string): number | null {
  const t = text.trim();
  if (t === "") return null;
  const n = Number(t);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

/**
 * Per-channel daily loss cap.
 *
 * Once a Telegram channel's realised P&L for today reaches minus its cap, new
 * automated entries from that channel are held until tomorrow. Other channels
 * keep trading and open positions are not touched. Held/not-held and today's
 * figures come from the backend, which reads them through the same function
 * the entry gate uses.
 *
 * An unreadable state shows a message, never "no caps": that would say
 * entries are allowed that may be held.
 */
export function ChannelLossCapCard({ cap, onSave }: ChannelLossCapCardProps) {
  if (cap === undefined) return null;
  if (cap === null) {
    return (
      <section className="rounded-lg border border-line bg-surface-1 p-3 text-xs text-warning">
        Channel daily loss caps could not be read.
      </section>
    );
  }

  const overrides = asObject(cap.overrides) as Record<string, number>;
  const rows = asArray<ChannelLossCapState["channels"][number]>(cap.channels);

  const saveOverride = (channel: string, text: string) => {
    const next = { ...overrides };
    const v = parseCap(text);
    if (v === null) delete next[channel];
    else next[channel] = v;
    onSave(cap.default_cap, next);
  };

  return (
    <section className="rounded-lg border border-line bg-surface-1 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-3">
        <h3 className="text-sm font-semibold text-ink-1">Channel daily loss cap</h3>
        <label className="text-xs text-ink-2">
          Cap for every channel ($)
          <Tooltip label="When a channel has lost this much today (net, closed trades), its new automated entries are held until tomorrow. Other channels keep trading; open positions are not touched. 0 turns it off.">
            <input
              aria-label="Cap for every channel"
              inputMode="decimal"
              defaultValue={String(cap.default_cap)}
              onBlur={(e) => onSave(parseCap(e.target.value) ?? 0, overrides)}
              className="num ml-2 w-24 rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
            />
          </Tooltip>
        </label>
        <span className="text-[11px] text-ink-3">0 turns this gate off</span>
      </div>
      {cap.error && <p className="mb-1 text-[11px] text-warning">{cap.error}</p>}

      {rows.length > 0 && (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-[11px] text-ink-3">
              <th className="py-1 font-normal">Channel</th>
              <th className="py-1 font-normal">Today</th>
              <th className="py-1 font-normal">Own cap ($, blank = default)</th>
              <th className="py-1 font-normal" />
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.channel} data-testid={`loss-cap-${r.channel}`}>
                <td className="py-1 text-ink-1">{r.channel}</td>
                <td className={cn("num py-1", r.day_pnl < 0 ? "text-loss" : "text-ink-2")}>
                  {formatSignedMoney(r.day_pnl)}
                </td>
                <td className="py-1">
                  <Tooltip label="This channel's own cap, replacing the default. Blank uses the default; 0 exempts this channel.">
                    <input
                      aria-label={`Cap for ${r.channel}`}
                      inputMode="decimal"
                      placeholder={String(cap.default_cap)}
                      defaultValue={r.channel in overrides ? String(overrides[r.channel]) : ""}
                      onBlur={(e) => saveOverride(r.channel, e.target.value)}
                      className="num w-20 rounded border border-line bg-surface-1 px-2 py-0.5 text-ink-1"
                    />
                  </Tooltip>
                </td>
                <td className="py-1">
                  {r.held ? (
                    <span className="rounded bg-warning/15 px-1.5 py-0.5 text-[11px] text-warning">
                      Held: cap {formatMoney(r.cap)} reached
                    </span>
                  ) : r.cap > 0 ? (
                    <span className="text-[11px] text-ink-3">cap {formatMoney(r.cap)}</span>
                  ) : (
                    <span className="text-[11px] text-ink-3">no cap</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
