import { useMemo, useState } from "react";
import { MessageSquareText } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { Tooltip } from "@/components/shared/Tooltip";
import { formatLots, formatPrice, formatUtcTime } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import { SignalEditorDialog } from "./SignalEditorDialog";

/**
 * Signals the engines and the Telegram reader have produced.
 *
 * Read-only here on purpose: acting on a signal opens a position, and that
 * control belongs with the rest of the money path rather than on a list row
 * where it is one mis-click from a trade.
 *
 * **It showed a direction, a status and four em dashes until 2026-09-21**
 * ("trading > signals - needs populating with details"). The rows are
 * `vantage_signals` columns — `source_name`, `entry_low`, `stop_loss`,
 * `lot_size` — and this read `source` and `entry`, which are not columns. The
 * route now fills the short names through `SignalOut`, and what a signal
 * actually says is on screen: when it arrived, the entry BAND rather than one
 * number, the stop, how many take-profits it carries, and the size it would be
 * opened at.
 *
 * The entry is a range, and it is drawn as one. A signal quoting 4370–4375
 * rendered as "4370" implies a precision the signal does not have, and that is
 * the number somebody would compare the live price against.
 */
interface SignalsSectionProps {
  signals: Record<string, unknown>[];
  onChanged: () => void;
}

/** How a status reads at a glance. Pending is the one that can still act. */
const STATUS_LOOK: Record<string, string> = {
  pending: "text-warning",
  active: "text-profit",
  activated: "text-profit",
  cancelled: "text-ink-3",
  expired: "text-ink-3",
};

function num(row: Record<string, unknown>, key: string): number | null {
  const raw = row[key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

/** Every take-profit the signal carries, in order, skipping the empty ones. */
function takeProfits(row: Record<string, unknown>): number[] {
  const out: number[] = [];
  for (let i = 1; i <= 8; i += 1) {
    const tp = num(row, `tp${i}`);
    if (tp !== null) out.push(tp);
  }
  return out;
}

/** "4370.00 – 4375.00", or just the one price when the band is a point. */
function entryBand(row: Record<string, unknown>): string {
  const low = num(row, "entry") ?? num(row, "entry_low");
  const high = num(row, "entry_high");
  if (low === null) return "—";
  if (high === null || Math.abs(high - low) < 0.005) return formatPrice(low);
  return `${formatPrice(low)} – ${formatPrice(high)}`;
}

export function SignalsSection({ signals, onChanged }: SignalsSectionProps) {
  const [editing, setEditing] = useState<Record<string, unknown> | null>(null);
  const [only, setOnly] = useState<string>("all");

  const statuses = useMemo(() => {
    const seen = new Set<string>();
    for (const s of signals) {
      const raw = s["status"];
      if (typeof raw === "string" && raw) seen.add(raw);
    }
    return [...seen].sort();
  }, [signals]);

  const shown = only === "all"
    ? signals
    : signals.filter((s) => String(s["status"] ?? "") === only);

  if (signals.length === 0) {
    return (
      <EmptyState
        title="No signals yet"
        hint="Engine and Telegram signals appear here as they are produced."
      />
    );
  }

  return (
    <>
      {statuses.length > 1 && (
        <div className="mb-2 flex items-center gap-1.5 text-[11px] text-ink-3">
          <span>showing</span>
          <Tooltip label="Every signal ever produced is kept. Narrow the list to the ones still waiting to act.">
            <select
              aria-label="Filter signals by status"
              value={only}
              onChange={(e) => setOnly(e.target.value)}
              className="rounded border border-line bg-surface-1 px-1.5 py-0.5 text-ink-1"
            >
              <option value="all">all statuses</option>
              {statuses.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </Tooltip>
          <span className="num">
            {shown.length} of {signals.length}
          </span>
        </div>
      )}

      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wide text-ink-3">
            <Th help="When the signal was produced, in UTC.">When</Th>
            <Th help="The engine or Telegram channel it came from.">Source</Th>
            <Th help="Buy or sell.">Side</Th>
            <Th help="The entry band the signal quotes. A signal gives a range, not a single price.">
              Entry
            </Th>
            <Th help="Where the position would be stopped out.">SL</Th>
            <Th help="Every take-profit the signal carries — up to eight. Hover a row's count to see them all.">
              TPs
            </Th>
            <Th help="The size it would be opened at, if the signal named one. Blank means the risk settings decide.">
              Lots
            </Th>
            <Th help="pending is still waiting to act on; anything else has already been decided.">
              Status
            </Th>
            <th className="py-1" />
          </tr>
        </thead>
        <tbody>
          {shown.map((s, i) => {
            const tps = takeProfits(s);
            const status = String(s["status"] ?? "—");
            const commentary = s["claude_commentary"];
            const notes = typeof s["notes"] === "string" ? s["notes"] : "";
            return (
              <tr
                key={String(s["id"] ?? s["signal_id"] ?? i)}
                data-testid="signal-row"
                className="border-t border-line"
              >
                <td className="num whitespace-nowrap py-1.5 text-[10px] text-ink-3">
                  {formatUtcTime(num(s, "created_at"))}
                </td>
                <td className="py-1.5 text-ink-2">
                  {String(s["source"] ?? s["source_name"] ?? s["channel"] ?? "—")}
                </td>
                <td className={s["direction"] === "SELL" ? "py-1.5 text-loss" : "py-1.5 text-profit"}>
                  {String(s["direction"] ?? "—")}
                </td>
                <td className="num whitespace-nowrap py-1.5 text-ink-1">{entryBand(s)}</td>
                <td className="num py-1.5 text-ink-3">{formatPrice(num(s, "sl"))}</td>
                <td className="num py-1.5 text-ink-3">
                  {tps.length === 0 ? "—" : (
                    <Tooltip label={tps.map((t) => formatPrice(t)).join(", ")}>
                      <span className="cursor-help underline decoration-dotted">
                        {tps.length} · {formatPrice(tps[0])}
                      </span>
                    </Tooltip>
                  )}
                </td>
                <td className="num py-1.5 text-ink-2">
                  {num(s, "lots") === null ? "—" : formatLots(num(s, "lots"))}
                </td>
                <td className={cn("py-1.5", STATUS_LOOK[status] ?? "text-ink-3")}>
                  {status}
                </td>
                <td className="py-1.5 text-right">
                  <span className="inline-flex items-center gap-1">
                    {notes && (
                      <Tooltip label={notes}>
                        <span className="cursor-help text-ink-3" aria-label="Notes on this signal">
                          <MessageSquareText size={12} aria-hidden />
                        </span>
                      </Tooltip>
                    )}
                    {commentary != null && commentary !== "" && (
                      <Tooltip label="The AI wrote a note about this signal. Open it with Edit.">
                        <span className="cursor-help text-accent">AI</span>
                      </Tooltip>
                    )}
                    <Button variant="ghost" onClick={() => setEditing(s)}>Edit</Button>
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {editing && (
        // Keyed on the signal id so the dialog REMOUNTS per row. Without it a
        // second Edit would reuse the first row's draft state — React's version
        // of the loop-capture bug the NiceGUI editor had.
        <SignalEditorDialog
          key={String(editing["signal_id"] ?? editing["id"] ?? "")}
          signal={editing}
          onClose={() => setEditing(null)}
          onSaved={onChanged}
        />
      )}
    </>
  );
}

/** A column heading that explains itself on hover. The tooltip wraps the span
 *  inside the cell, not the cell: `<tr>` may only contain `<th>` and `<td>`. */
function Th({ help, children }: { help: string; children: React.ReactNode }) {
  return (
    <th className="py-1 font-medium">
      <Tooltip label={help} side="bottom">
        <span className="cursor-help">{children}</span>
      </Tooltip>
    </th>
  );
}
