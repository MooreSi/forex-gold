import { formatMoney, formatPrice } from "@/components/shared/format";

export interface PositionOverlayProps {
  direction: "BUY" | "SELL";
  entry: number;
  stopLoss: number;
  takeProfit: number;
  /** Pixel y of each level, from the chart's own price scale. */
  entryY: number;
  stopY: number;
  targetY: number;
  /** Pixel x of the box's two edges. */
  left: number;
  right: number;
  /** Cash at each end for the chosen lot size. Null until one is chosen. */
  riskMoney: number | null;
  rewardMoney: number | null;
  rr: number | null;
}

/**
 * TradingView's long/short position tool, drawn over the chart.
 *
 * **Pure.** Every number arrives already computed — the prices from the
 * backend's candidate, the pixel positions from the chart's own price scale,
 * the cash from a per-lot figure the backend derived with `fees_sizing.pnl`.
 * Nothing here calculates what a trade is worth, which is why it can be
 * rendered and asserted without a chart engine underneath it.
 *
 * The geometry is direction-agnostic on purpose. A long has its target above
 * the entry and a short below, and writing the bands as "profit side" and
 * "loss side" rather than "upper" and "lower" is what stops a short rendering
 * with its colours inverted — a picture that reads as the opposite trade.
 */
export function PositionOverlay(props: PositionOverlayProps) {
  const {
    direction, entry, stopLoss, takeProfit,
    entryY, stopY, targetY, left, right,
    riskMoney, rewardMoney, rr,
  } = props;

  const width = Math.max(right - left, 0);
  const profitTop = Math.min(entryY, targetY);
  const profitHeight = Math.abs(targetY - entryY);
  const lossTop = Math.min(entryY, stopY);
  const lossHeight = Math.abs(stopY - entryY);

  const pct = (level: number) => ((level - entry) / entry) * 100;
  const points = (level: number) => Math.abs(level - entry);

  return (
    <div
      data-testid="position-overlay"
      aria-hidden
      // z-20: lightweight-charts stacks its own panes and canvases up to a
      // z-index of about 3 inside the holder. Without an explicit one here the
      // box is laid out perfectly and painted UNDERNEATH the candles, which
      // looks exactly like it failed to render at all.
      className="pointer-events-none absolute inset-0 z-20 overflow-hidden"
    >
      <div
        className="absolute rounded-sm bg-profit/15 ring-1 ring-inset ring-profit/30"
        style={{ left, top: profitTop, width, height: profitHeight }}
      />
      <div
        className="absolute rounded-sm bg-loss/15 ring-1 ring-inset ring-loss/30"
        style={{ left, top: lossTop, width, height: lossHeight }}
      />

      {/* The entry line, with a handle at each end the way the tool draws it. */}
      <div
        className="absolute h-px bg-ink-1/70"
        style={{ left, top: entryY, width }}
      />
      <Handle x={left} y={entryY} />
      <Handle x={right} y={entryY} />
      <Handle x={left + width / 2} y={targetY} />
      <Handle x={left + width / 2} y={stopY} />

      <Tag
        tone="profit"
        x={left + width / 2}
        y={targetY}
        place="above"
        label="Target"
        price={takeProfit}
        pct={pct(takeProfit)}
        points={points(takeProfit)}
        money={rewardMoney}
      />
      <Tag
        tone="loss"
        x={left + width / 2}
        y={stopY}
        place="below"
        label="Stop"
        price={stopLoss}
        pct={pct(stopLoss)}
        points={points(stopLoss)}
        money={riskMoney}
      />

      <div
        className="absolute -translate-x-1/2 translate-y-1.5 whitespace-nowrap rounded
                   bg-surface-3/95 px-2 py-1 text-center text-[10px] leading-tight
                   text-ink-1 shadow-lg ring-1 ring-line"
        style={{ left: left + width / 2, top: entryY }}
      >
        <span className="num">
          {direction} entry {formatPrice(entry)}
        </span>
        <br />
        <span className="text-ink-3">
          Risk/reward {rr ? `1:${rr.toFixed(2)}` : "—"}
        </span>
      </div>
    </div>
  );
}

function Handle({ x, y }: { x: number; y: number }) {
  return (
    <div
      className="absolute h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-[2px]
                 border border-remote bg-surface-0"
      style={{ left: x, top: y }}
    />
  );
}

interface TagProps {
  tone: "profit" | "loss";
  x: number;
  y: number;
  place: "above" | "below";
  label: string;
  price: number;
  pct: number;
  points: number;
  money: number | null;
}

/**
 * One of the two price flags.
 *
 * The cash amount is omitted rather than shown as $0.00 when no lot size has
 * been chosen. Zero is a real answer meaning "this trade wins nothing", and it
 * is not the same as not having decided how much to trade.
 */
function Tag({ tone, x, y, place, label, price, pct, points, money }: TagProps) {
  const toneClass = tone === "profit"
    ? "bg-profit/90 text-surface-0"
    : "bg-loss/90 text-surface-0";
  const offset = place === "above" ? "-translate-y-[calc(100%+6px)]" : "translate-y-1.5";
  return (
    <div
      className={`absolute -translate-x-1/2 ${offset} whitespace-nowrap rounded px-2
                  py-0.5 text-[10px] font-medium shadow-lg ${toneClass}`}
      style={{ left: x, top: y }}
    >
      <span className="num">
        {label}: {formatPrice(price)} ({pct >= 0 ? "+" : ""}{pct.toFixed(2)}%)
        {" · "}{points.toFixed(2)} pts
        {money !== null && <> · {formatMoney(money)}</>}
      </span>
    </div>
  );
}
