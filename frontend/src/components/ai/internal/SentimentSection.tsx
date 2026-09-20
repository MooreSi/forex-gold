import { Minus, TrendingDown, TrendingUp } from "lucide-react";
import type { MarketAnalysis } from "../hooks/useMarketResearch";

/** How the three sentiments look. Profit/loss colours, as everywhere else. */
const TONES: Record<string, { text: string; edge: string; Icon: typeof TrendingUp }> = {
  bullish: { text: "text-profit", edge: "border-profit/40 bg-profit/10", Icon: TrendingUp },
  bearish: { text: "text-loss", edge: "border-loss/40 bg-loss/10", Icon: TrendingDown },
};
const NEUTRAL = { text: "text-ink-2", edge: "border-line bg-surface-2", Icon: Minus };

/**
 * The model's call, and how much weight it puts behind it.
 *
 * The confidence is never optional decoration. "Bullish" on its own is a
 * claim with nothing behind it, and this screen is one tab away from a Place
 * Order button.
 */
export function SentimentSection({ analysis }: { analysis: MarketAnalysis }) {
  const sentiment = (analysis.sentiment || "neutral").toLowerCase();
  const tone = TONES[sentiment] ?? NEUTRAL;
  const raw = Number(analysis.sentiment_confidence ?? 0);
  const pct = Math.max(0, Math.min(100, Math.round((Number.isFinite(raw) ? raw : 0) * 100)));
  const bar = pct >= 65 ? "bg-profit" : pct >= 40 ? "bg-warning" : "bg-loss";

  return (
    <div className={`rounded-lg border p-4 ${tone.edge}`}>
      <h3 className="text-[10px] font-semibold uppercase tracking-wider text-ink-3">
        Market sentiment
      </h3>
      <div className="mt-1.5 flex items-center gap-2">
        <tone.Icon size={24} className={tone.text} />
        <span data-testid="sentiment" className={`text-2xl font-bold ${tone.text}`}>
          {sentiment.toUpperCase()}
        </span>
      </div>
      <div className="mt-2 flex items-center gap-2">
        <span data-testid="confidence" className="num shrink-0 text-[11px] text-ink-3">
          Confidence: {pct}%
        </span>
        <div className="h-2 flex-1 rounded-full bg-surface-3">
          <div className={`h-2 rounded-full ${bar}`} style={{ width: `${pct}%` }} />
        </div>
      </div>
      {analysis.today_bias && (
        <p className="mt-2 text-xs italic text-ink-2">{analysis.today_bias}</p>
      )}
    </div>
  );
}
