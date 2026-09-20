import { asArray } from "@/lib/asArray";
import { plotPoints, rollingMean, WINDOW } from "./learning_series";

interface LearningChartSectionProps {
  /** `ml.metrics` from either engine's report. Shapes match. */
  metrics: Record<string, unknown>;
  /** `ml.summary`, for the "needs N more" line when there is nothing yet. */
  summary: Record<string, unknown>;
}

const W = 520;
const H = 90;

// The table below this chart colours PREDICTED R orange and actual R purple.
// The NiceGUI legend called orange "actual R" — same colour, two meanings,
// one card. These are the table's colours, deliberately.
const WIN_RATE = "#4ade80";
const ACTUAL_R = "#c084fc";

function num(value: unknown): number | null {
  if (value == null || value === "") return null;
  const v = Number(value);
  return Number.isFinite(v) ? v : null;
}

/**
 * "Is it learning?" — the rolling win rate and realised R over closed signals.
 *
 * **The two lines are not comparable to each other**, and the card says so on
 * its own face. Win rate is a percentage on 0-100; mean realised R is on
 * -1..+1. Each is read against the dashed midline — 50% and 0.0 respectively.
 * A rising win rate with a falling R is both possible and important: it is
 * what "more small wins, fewer but larger losses" looks like.
 *
 * The x axis is closed signals, oldest to newest. It is **not time**: signals
 * are not evenly spaced, and reading it as a time series is the most likely
 * wrong conclusion to draw from it.
 */
export function LearningChartSection({ metrics, summary }: LearningChartSectionProps) {
  const ids = asArray<unknown>(metrics["signal_ids"]);
  const winFlags = asArray<number | null>(metrics["win_flag_series"]);
  const actualR = asArray<number | null>(metrics["actual_r_series"]);

  if (ids.length === 0 || winFlags.length === 0) {
    const labelled = num(summary["labeled_count"]);
    const needed = num(summary["min_needed"]);
    const remaining = labelled != null && needed != null
      ? Math.max(0, needed - labelled)
      : null;
    return (
      <div className="rounded border border-line bg-surface-2 px-3 py-2">
        <p className="text-[11px] italic text-ink-3">
          No closed signals with a stored ML probability yet, so there is
          nothing to plot.
        </p>
        {/* Without this, "nothing to plot" reads as a broken panel rather
            than an engine that has not finished gathering evidence. */}
        {remaining != null && remaining > 0 && (
          <p className="mt-0.5 text-[11px] text-ink-2">
            Needs {remaining} more closed signals before the first training run.
          </p>
        )}
      </div>
    );
  }

  const winRate = rollingMean(winFlags).map((v) => v * 100);
  const meanR = rollingMean(actualR);
  const winPts = plotPoints(winRate, 0, 100, W, H);
  const rPts = plotPoints(meanR, -1, 1, W, H);

  return (
    <div className="rounded border border-line bg-surface-2 p-3">
      <h4 className="text-[10px] font-semibold uppercase tracking-wider text-ink-3">
        Is it learning? (rolling {WINDOW} closed signals)
      </h4>
      <svg
        role="img"
        aria-label="Rolling win rate and realised R over closed signals"
        viewBox={`0 0 ${W} ${H}`}
        className="mt-1.5 h-24 w-full rounded bg-surface-1"
        preserveAspectRatio="none"
      >
        <line
          x1="0" y1={H / 2} x2={W} y2={H / 2}
          stroke="currentColor" strokeWidth="1" strokeDasharray="4,4"
          className="text-line"
        />
        {winPts && (
          <polyline
            data-testid="learning-win-rate"
            points={winPts} fill="none" stroke={WIN_RATE} strokeWidth="1.5"
            vectorEffect="non-scaling-stroke"
          />
        )}
        {rPts && (
          <polyline
            data-testid="learning-actual-r"
            points={rPts} fill="none" stroke={ACTUAL_R} strokeWidth="1.5"
            strokeDasharray="3,2" vectorEffect="non-scaling-stroke"
          />
        )}
      </svg>
      <div className="mt-1 flex flex-wrap gap-3 text-[11px]">
        <span style={{ color: WIN_RATE }}>— win rate, 0-100% scale</span>
        <span style={{ color: ACTUAL_R }}>--- mean realised R, -1..+1 scale</span>
      </div>
      <p className="mt-0.5 text-[10px] leading-relaxed text-ink-3">
        X: closed signals, oldest to newest (not time — signals are not evenly
        spaced). The dashed midline is 50% for win rate and 0.0 for R. The two
        lines use different scales and are not comparable to each other; read
        each against the midline.
      </p>
    </div>
  );
}
