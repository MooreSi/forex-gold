import { formatMoney } from "@/components/shared/format";

/**
 * The day's range drawn to scale, with the levels the model is watching.
 *
 * Resistance hangs above the track and support sits below it, the way they sit
 * on a chart. The scale runs from the lowest to the highest number the model
 * gave, so a level outside the expected range is visibly outside it.
 *
 * Positioned boxes rather than an SVG: the ladder stretches to the card's
 * width, and a stretched SVG stretches its markers with it.
 */
export function RangeLadder({ low, high, supports, resistances }: {
  low: number; high: number; supports: number[]; resistances: number[];
}) {
  const all = [low, high, ...supports, ...resistances].filter((v) => v > 0);
  const lo = Math.min(...all);
  const hi = Math.max(...all);
  if (!(hi > lo) || !(low > 0) || !(high > 0)) return null;

  // 3% in from each end, so a marker at the extreme is not clipped.
  const at = (v: number) => `${3 + ((v - lo) / (hi - lo)) * 94}%`;
  const from = Math.min(low, high);
  const to = Math.max(low, high);

  return (
    <div
      role="img"
      aria-label={`Today's range ${formatMoney(low)} to ${formatMoney(high)}, `
        + `${supports.length} support and ${resistances.length} resistance levels`}
      className="relative h-12 w-full"
    >
      <div className="absolute inset-x-0 top-1/2 h-0.5 -translate-y-1/2 rounded-full bg-line" />
      <div
        className="absolute top-1/2 h-2.5 -translate-y-1/2 rounded-full bg-gradient-to-r
                   from-loss via-accent to-profit opacity-85 shadow-sm"
        style={{ left: at(from), width: `calc(${at(to)} - ${at(from)})` }}
      />
      {resistances.map((v) => (
        <span
          key={`r${v}`}
          data-level="resistance"
          className="absolute top-1 size-0 -translate-x-1/2 border-x-[5px] border-t-[7px]
                     border-x-transparent border-t-loss"
          style={{ left: at(v) }}
        />
      ))}
      {supports.map((v) => (
        <span
          key={`s${v}`}
          data-level="support"
          className="absolute bottom-1 size-0 -translate-x-1/2 border-x-[5px] border-b-[7px]
                     border-x-transparent border-b-profit"
          style={{ left: at(v) }}
        />
      ))}
    </div>
  );
}
