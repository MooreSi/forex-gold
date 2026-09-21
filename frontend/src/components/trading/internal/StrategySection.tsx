import { useEffect, useMemo, useState } from "react";
import { Sparkles, Wand2 } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { Tooltip } from "@/components/shared/Tooltip";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatMoney, pnlColour } from "@/components/shared/format";
import { cn } from "@/lib/cn";
import { useStrategyController } from "../hooks/useStrategyController";

/**
 * Which strategy each signal source trades under.
 *
 * Never ported until 2026-09-19. A template decides how ONE trade is managed
 * by the EA; this decides which management a whole CHANNEL gets, and it is the
 * screen an operator uses after reading the channel scorecard on Analysis.
 *
 * **Auto and an override are different things and the screen says which is in
 * force.** Auto lets the app pick from that channel's own record; an override
 * pins it. A row showing a strategy name with no indication of which of those
 * produced it is a row you cannot act on.
 *
 * The AI recommendation is split in two, as the endpoints are: the free one
 * reads what was computed before, and the paid one is behind a button that
 * says it costs money. Every other AI surface in this app follows the same
 * rule.
 */
export function StrategySection() {
  const c = useStrategyController();
  const [asked, setAsked] = useState(false);

  const sources = useMemo(() => c.channels.map((ch) => ch.source), [c.channels]);

  useEffect(() => {
    // The free read, once the channel list is known. Free because it is a
    // read of the local record -- nothing is billed until the button below.
    if (sources.length && !asked) {
      setAsked(true);
      void c.loadRecommendations(sources);
    }
  }, [sources, asked, c]);

  const labelOf = (key: string | null) =>
    c.catalogue.find((s) => s.key === key)?.label ?? key ?? "";

  if (c.loading) return <EmptyState title="Loading the channel strategies" />;
  if (!c.channels.length) {
    return (
      <EmptyState
        title={c.error ? "Could not load the channel strategies" : "No signal sources yet"}
        hint={c.error?.message
          ?? "A channel appears here once it has sent a signal the parser understood."}
      />
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-[11px] text-ink-3">
          A template decides how one trade is managed. This decides which
          management a whole channel gets.
        </p>
        <Button
          variant="ghost"
          className="ml-auto"
          disabled={c.busy === "recommend"}
          onClick={() => void c.requestRecommendations(sources)}
          title="Asks the configured AI model. This costs money."
          tooltip="Asks the configured AI model to re-pick a strategy for every channel marked Auto, from that channel's own trading record. Billable, and it changes nothing until you apply a recommendation."
        >
          <Sparkles size={13} />
          {c.busy === "recommend" ? "Asking…" : "Ask the AI (billable)"}
        </Button>
      </div>

      {c.refusal && (
        <p role="alert" className="rounded border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
          {c.refusal}
        </p>
      )}

      <div className="overflow-auto">
        <table className="w-full min-w-[46rem] text-left text-[11px]">
          <thead className="text-ink-3">
            <tr>
              {["Channel", "In force", "Strategy", "Auto", "Lots", "Record", "Suggested"]
                .map((h) => <th key={h} className="px-2 py-1 font-normal">{h}</th>)}
            </tr>
          </thead>
          <tbody>
            {c.channels.map((ch) => {
              const rec = c.recs[ch.source];
              const pinned = Boolean(ch.strategy_override);
              return (
                <tr key={ch.source} data-testid={`strategy-${ch.source}`}
                  className="border-t border-line">
                  <td className="px-2 py-1.5 text-ink-1">{ch.source}</td>
                  <td className="px-2 py-1.5">
                    {/* Which mechanism decided, not just what it decided. */}
                    <span className={cn(
                      "rounded px-1.5 py-0.5 text-[10px]",
                      pinned ? "bg-accent/15 text-accent" : "bg-surface-3 text-ink-3",
                    )}>
                      {pinned ? "pinned" : ch.auto_strategy ? "auto" : "default"}
                    </span>
                  </td>
                  <td className="px-2 py-1.5">
                    <Tooltip label={`How a position from ${ch.source} is managed once it is open — where the stop moves, how the size is scaled out, what the targets are. "Use the default" leaves it on the app's own choice. A schedule window can override this again for its own hours.`}>
                    <select
                      aria-label={`Strategy for ${ch.source}`}
                      value={ch.strategy_override ?? ""}
                      disabled={c.busy === ch.source}
                      onChange={(e) => void c.assign(
                        ch.source, e.target.value || null, ch.auto_strategy)}
                      className="w-48 rounded border border-line bg-surface-1 px-2 py-1 text-[11px] text-ink-1"
                    >
                      <option value="">— use the default —</option>
                      {c.catalogue.map((s) => (
                        <option key={s.key} value={s.key}>{s.label}</option>
                      ))}
                    </select>
                    </Tooltip>
                  </td>
                  <td className="px-2 py-1.5">
                    <Tooltip label={`Let the AI re-pick ${ch.source}'s strategy from its own record when you press Evaluate. Off, the strategy stays exactly as set here until you change it yourself. It never changes anything on its own.`}>
                      <input
                        type="checkbox"
                        aria-label={`Auto strategy for ${ch.source}`}
                        checked={ch.auto_strategy}
                        disabled={c.busy === ch.source}
                        onChange={(e) => void c.assign(
                          ch.source, ch.strategy_override, e.target.checked)}
                        className="size-3.5 accent-[var(--color-accent)]"
                      />
                    </Tooltip>
                  </td>
                  <td className="num px-2 py-1.5 text-ink-3">
                    {Number(ch.lot_mult ?? 1).toFixed(2)}x
                  </td>
                  <td className="num px-2 py-1.5 text-ink-3">
                    {ch.sample_n
                      ? <>
                          {Number(ch.win_rate ?? 0).toFixed(0)}% of {ch.sample_n}
                          {" · "}
                          <span className={pnlColour(ch.net_pnl ?? 0)}>
                            {formatMoney(ch.net_pnl ?? 0)}
                          </span>
                        </>
                      // Not "0% of 0". A channel with no closed trades has no
                      // record, and a zero reads as a channel that never wins.
                      : "no trades yet"}
                  </td>
                  <td className="px-2 py-1.5 text-ink-2">
                    {rec?.strategy
                      ? (
                        <button
                          type="button"
                          onClick={() => void c.assign(ch.source, rec.strategy!, false)}
                          title={rec.summary || rec.reason || "Apply this recommendation"}
                          className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-remote hover:bg-surface-2"
                        >
                          <Wand2 size={11} />
                          {rec.label || labelOf(rec.strategy)}
                        </button>
                      )
                      : <span className="text-ink-3">—</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
