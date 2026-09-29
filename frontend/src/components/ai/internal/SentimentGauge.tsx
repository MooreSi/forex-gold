/**
 * A half-dial for the model's call: bearish on the left, bullish on the right.
 *
 * The needle's lean is the confidence, not a second opinion. A bearish call at
 * 60% points 60% of the way from the centre to the bearish end; neutral sits
 * upright whatever the confidence. The label says the same thing in words, so
 * the picture is never the only place the call is stated.
 */

const CX = 60;
const CY = 60;
const R = 48;

function point(deg: number, r = R): [number, number] {
  const rad = (deg * Math.PI) / 180;
  return [CX + r * Math.cos(rad), CY - r * Math.sin(rad)];
}

function arc(from: number, to: number): string {
  const [x1, y1] = point(from);
  const [x2, y2] = point(to);
  return `M${x1.toFixed(2)} ${y1.toFixed(2)} A${R} ${R} 0 0 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
}

/** Three bands, a hair apart so they read as zones rather than one stroke. */
const BANDS = [
  { from: 180, to: 122, tone: "text-loss" },
  { from: 118, to: 62, tone: "text-warning" },
  { from: 58, to: 0, tone: "text-profit" },
];

export function SentimentGauge({ sentiment, pct }: { sentiment: string; pct: number }) {
  const lean = sentiment === "bullish" ? pct / 100 : sentiment === "bearish" ? -pct / 100 : 0;
  const angle = 90 - lean * 90;
  const [nx, ny] = point(angle, R - 10);

  return (
    <svg
      role="img"
      aria-label={`Sentiment gauge: ${sentiment}, ${pct}% confidence`}
      viewBox="0 0 120 70"
      className="h-20 w-32 shrink-0"
    >
      {/* A faint wide track behind the bands, so the dial has depth. */}
      <path d={arc(180, 0)} className="text-ink-3"
        stroke="currentColor" strokeOpacity={0.12} strokeWidth={20} fill="none" />
      {BANDS.map((b) => (
        <path
          key={b.from}
          d={arc(b.from, b.to)}
          className={b.tone}
          stroke="currentColor"
          strokeOpacity={0.85}
          strokeWidth={9}
          fill="none"
        />
      ))}
      <line
        x1={CX} y1={CY} x2={nx} y2={ny}
        className="text-ink-1" stroke="currentColor" strokeWidth={2.5} strokeLinecap="round"
      />
      <circle cx={CX} cy={CY} r={4.5} className="text-ink-1" fill="currentColor" />
      <circle cx={CX} cy={CY} r={1.8} className="text-surface-2" fill="currentColor" />
    </svg>
  );
}
