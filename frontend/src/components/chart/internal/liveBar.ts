export interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

/**
 * The last bar after one tick, or null when the tick should not touch it.
 *
 * Moves the forming candle between the 10 s candle refreshes, so the chart is
 * as live as the 1 s tick. MT5 builds its bars from the bid, so the bid is
 * used. Candle and tick timestamps are both broker server time, so they can be
 * compared directly.
 *
 * `prev` is the bar currently drawn last, which is the previous result once
 * ticks have started a new bar. Null for a tick older than it: lightweight-charts
 * throws on an update earlier than its last bar.
 */
export function liveBar(
  prev: Bar,
  tick: { bid: number; timestamp: number },
  tfSeconds: number,
): Bar | null {
  const { bid, timestamp } = tick;
  if (!Number.isFinite(bid) || !Number.isFinite(timestamp) || timestamp <= 0) return null;
  if (!(tfSeconds > 0)) return null;
  const bucket = Math.floor(timestamp / tfSeconds) * tfSeconds;
  if (bucket < prev.time) return null;
  if (bucket === prev.time) {
    return {
      ...prev,
      high: Math.max(prev.high, bid),
      low: Math.min(prev.low, bid),
      close: bid,
    };
  }
  return { time: bucket, open: bid, high: bid, low: bid, close: bid };
}
