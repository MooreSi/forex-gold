import { useMemo } from "react";
import { cn } from "@/lib/cn";
import { ladderLevels, ladderRr } from "./ladderRr";

interface LadderRrSectionProps {
  /** "tp" for the anchor ladder, "tp_pen" for the pending one. */
  prefix: "tp" | "tp_pen";
  /** How many levels this ladder has, from the backend's own field list. */
  maxLevel: number;
  /** The editor's live draft. Numbers are strings while being edited. */
  draft: Record<string, unknown>;
}

/**
 * Reward per unit of risk for one TP ladder, under the ladder itself.
 *
 * A ladder is a set of distances and percentages; what it is WORTH depends on
 * the stop it runs against, and that is three fields further up the form. The
 * original dashboard put this readout under both ladders for exactly that
 * reason, and the React port shipped without it — so a template could be
 * designed only by eye.
 *
 * Two rows, because they answer different questions:
 *
 *   **R at TP** is how far that level reaches — pips over the stop. It does
 *   not move when the % beneath it changes.
 *
 *   **weighted R** is what the level actually contributes once only its own
 *   slice of the position is banked there.
 *
 * A ladder can look generous on the first row and give most of it back on the
 * second: a level reaching 2.50R contributes 0.25R if only 10% is still open
 * by the time price arrives. The summary line is the one that answers "is this
 * strategy worth trading" — the total is what the geometry pays if price
 * reaches every configured level, which is deliberately not the same claim as
 * how often it will.
 *
 * The arithmetic is in `ladderRr.ts`, which is the same rule as the backend's
 * `ladder_rr` and is pinned against it by a shared case file. Read that file's
 * header before changing any number that comes out of here.
 */
export function LadderRrSection({ prefix, maxLevel, draft }: LadderRrSectionProps) {
  const sl = Number(draft["sl_pips"]);
  const closeFull = Boolean(draft["close_full_on_last"]);
  const fromSignal = Boolean(
    draft[prefix === "tp" ? "tp_from_telegram" : "tp_pen_from_telegram"]);

  const { levels, calc } = useMemo(() => {
    const lv = ladderLevels(draft, prefix, maxLevel);
    return { levels: lv, calc: ladderRr(sl, lv, closeFull) };
  }, [draft, prefix, maxLevel, sl, closeFull]);

  const byLevel = new Map(calc.rows.map((r) => [r.level, r]));
  const columns = Array.from({ length: maxLevel }, (_, i) => i + 1);
  const over = calc.pctSum > 100;

  let summary: string;
  if (!Number.isFinite(sl) || sl <= 0) {
    summary = "Set a stop distance above to see this ladder's R:R.";
  } else if (levels.length === 0) {
    summary = "No take-profit level is set, so there is nothing to reach.";
  } else {
    const banked = 100 - calc.remaining;
    const bits = [`Total ${calc.totalR.toFixed(2)}R if every level is hit`];
    if (over) {
      bits.push(`the %s add to ${calc.pctSum.toFixed(0)}% — a level can only `
        + "close what earlier ones left");
    } else if (calc.remaining > 0) {
      bits.push(`${calc.remaining.toFixed(0)}% left running on the trail/SL`);
    }
    if (fromSignal) {
      bits.push("the distances come from the signal — this reads the boxes above");
    }
    summary = `${bits.join("  •  ")}  (closes ${banked.toFixed(0)}%)`;
  }

  const showTable = Number.isFinite(sl) && sl > 0 && levels.length > 0;

  return (
    <div className="mt-1.5 rounded border border-line bg-surface-2 px-2 py-1.5">
      {showTable && (
        <table className="w-full table-fixed border-collapse text-[10px]">
          <thead>
            <tr>
              <th className="w-24 text-left font-normal text-ink-3" />
              {columns.map((n) => (
                <th key={n} className="font-normal text-ink-3">
                  TP{n}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row" className="text-left font-normal text-ink-3">R at TP</th>
              {columns.map((n) => (
                <td key={n} className="text-center text-ink-2">
                  {byLevel.has(n) ? `${byLevel.get(n)!.rr.toFixed(2)}R` : "—"}
                </td>
              ))}
            </tr>
            <tr>
              <th scope="row" className="text-left font-normal text-ink-3">weighted R</th>
              {columns.map((n) => {
                const row = byLevel.get(n);
                return (
                  <td key={n} className="text-center text-profit">
                    {row && row.closed > 0 ? `${row.contribution.toFixed(2)}R` : "—"}
                  </td>
                );
              })}
            </tr>
          </tbody>
        </table>
      )}
      <p
        data-testid={`rr-summary-${prefix}`}
        className={cn("text-[10px]", showTable && "mt-1",
          !showTable ? "text-ink-3" : over ? "text-warning" : "text-profit")}
      >
        {summary}
      </p>
    </div>
  );
}
