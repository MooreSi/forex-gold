/**
 * The retracement band, and the instrument readout beside it.
 *
 * The band is the one drawn thing that the checklist also SCORES, so the
 * failure that matters is the two disagreeing: a band whose edges are not the
 * band `confluence.FIB_LOW`/`FIB_HIGH` bound would show price inside the zone
 * while the checklist said it was outside, with nothing on screen saying which
 * is right. Every price here is computed by the backend's
 * `retracement_price` — the exact inverse of the scoring function — and this
 * file only checks that what arrives is what is drawn.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FibonacciOverlay } from "../internal/FibonacciOverlay";
import { IndicatorStrip } from "../internal/IndicatorStrip";
import type { SetForgetEvidence } from "@/api/types";

/**
 * Four levels, 38.2% highest on screen down to 78.6% lowest, spread far
 * enough apart that every one of them is legible. The crowded case has its
 * own fixture at the bottom of this file.
 */
const FIBS = [
  { ratio: 0.382, price: 1990, y: 20 },
  { ratio: 0.5, price: 1985, y: 40 },
  { ratio: 0.618, price: 1980, y: 60 },
  { ratio: 0.786, price: 1973, y: 84 },
];

function band() {
  return [...screen.getByTestId("fibonacci-overlay").children]
    .find((d) => d.className.includes("border-dashed")) as HTMLElement;
}

describe("the retracement band", () => {
  it("spans the outermost two levels, which are the band the checklist scores", () => {
    render(<FibonacciOverlay fibs={FIBS} />);

    expect(band().style.top).toBe("20px");
    expect(band().style.height).toBe("64px");
  });

  it("labels every level with its percentage and its price", () => {
    render(<FibonacciOverlay fibs={FIBS} />);

    expect(screen.getByText("61.8% · 1980.00")).toBeInTheDocument();
    expect(screen.getByText("38.2% · 1990.00")).toBeInTheDocument();
    expect(screen.getByText("78.6% · 1973.00")).toBeInTheDocument();
  });

  it("draws nothing at all when no leg has completed", () => {
    /* A band at zero, across the bottom of the chart, looks like a real level
       nobody can account for. Absent is the honest rendering of absent. */
    render(<FibonacciOverlay fibs={[]} />);

    expect(screen.queryByTestId("fibonacci-overlay")).not.toBeInTheDocument();
  });

  it("sits beneath the areas of interest and the position box", () => {
    /* It is context, not a level anything is placed at. jsdom has no layout,
       so the stacking class is the only observable — see SetupChart.test. */
    render(<FibonacciOverlay fibs={FIBS} />);

    expect(screen.getByTestId("fibonacci-overlay").className).toMatch(/\bz-0\b/);
  });

  it("copes with a single surviving level rather than collapsing", () => {
    render(<FibonacciOverlay fibs={[FIBS[1]]} />);

    expect(band().style.height).toBe("0px");
    expect(screen.getByText("50.0% · 1985.00")).toBeInTheDocument();
  });
});

const EVIDENCE: SetForgetEvidence = {
  price: 2000, weekly_bias: "bullish", daily_bias: "bullish",
  entry_bias: "bullish", entry_timeframe: "4H", zones: [],
  atr: 6.4, ema_fast: 1995, ema_slow: 1960, rsi: 52, fib: 0.618,
  fib_levels: [], impulse: null, confirmation: null,
};

describe("the instrument readout", () => {
  it("prints every instrument the method reads", () => {
    render(<IndicatorStrip evidence={EVIDENCE} />);

    expect(screen.getByText("1995.00")).toBeInTheDocument();     // EMA 50
    expect(screen.getByText("1960.00")).toBeInTheDocument();     // EMA 200
    expect(screen.getByText("52.0")).toBeInTheDocument();        // RSI
    expect(screen.getByText("6.40")).toBeInTheDocument();        // ATR
    expect(screen.getByText("61.8%")).toBeInTheDocument();       // pullback
  });

  it("says which side of the slow average the fast one is on", () => {
    render(<IndicatorStrip evidence={EVIDENCE} />);

    expect(screen.getByText("above 200")).toBeInTheDocument();
  });

  it("says below when the trend is the other way", () => {
    render(<IndicatorStrip evidence={{ ...EVIDENCE, ema_fast: 1950 }} />);

    expect(screen.getByText("below 200")).toBeInTheDocument();
  });

  it("colours an overbought RSI as a problem and names it", () => {
    render(<IndicatorStrip evidence={{ ...EVIDENCE, rsi: 78 }} />);

    expect(screen.getByText("overbought")).toBeInTheDocument();
    expect(screen.getByText("78.0").className).toContain("text-loss");
  });

  it("names an oversold one too", () => {
    render(<IndicatorStrip evidence={{ ...EVIDENCE, rsi: 21 }} />);

    expect(screen.getByText("oversold")).toBeInTheDocument();
  });

  it("shows an unreadable value as a dash, never as zero", () => {
    /* An EMA of 0.00 on gold is not a number anyone should be shown as if it
       were real, and a reader who believes it concludes the trend is up. */
    render(<IndicatorStrip evidence={{
      ...EVIDENCE, ema_fast: null, ema_slow: null, rsi: null, atr: 0, fib: null,
    }} />);

    expect(screen.getAllByText("—")).toHaveLength(5);
    expect(screen.queryByText("0.00")).not.toBeInTheDocument();
    expect(screen.getByText("no completed leg")).toBeInTheDocument();
  });

  it("names the timeframe the instruments were read on", () => {
    render(<IndicatorStrip evidence={EVIDENCE} />);

    expect(screen.getByText(/Instruments on the 4H chart/)).toBeInTheDocument();
  });
});

describe("when the levels are crowded together", () => {
  /** A short leg on a wide scale: four levels inside twenty pixels. */
  const TIGHT = [
    { ratio: 0.382, price: 1990, y: 100 },
    { ratio: 0.5, price: 1988, y: 108 },
    { ratio: 0.618, price: 1986, y: 116 },
    { ratio: 0.786, price: 1983, y: 129 },
  ];

  it("still draws every line", () => {
    /* The lines are the reading; the labels are the convenience. Dropping a
       level entirely would change what the chart says the band is. */
    render(<FibonacciOverlay fibs={TIGHT} />);

    const lines = [...screen.getByTestId("fibonacci-overlay").children]
      .filter((d) => !d.className.includes("border-dashed"));

    expect(lines).toHaveLength(4);
  });

  it("drops the labels that would land on top of each other", () => {
    render(<FibonacciOverlay fibs={TIGHT} />);

    expect(screen.getAllByText(/% · /).length).toBeLessThan(4);
    expect(screen.getByText("38.2% · 1990.00")).toBeInTheDocument();
    expect(screen.queryByText("50.0% · 1988.00")).not.toBeInTheDocument();
  });

  it("keeps the ones that do have room", () => {
    render(<FibonacciOverlay fibs={TIGHT} />);

    expect(screen.getByText("61.8% · 1986.00")).toBeInTheDocument();
    expect(screen.getByText("78.6% · 1983.00")).toBeInTheDocument();
  });

  it("thins a short's levels by screen position, not by ratio", () => {
    /* A short's leg runs high to low, so its 78.6% is ABOVE its 38.2% on
       screen. A sweep in ratio order would compare each label against one
       nowhere near it and thin the wrong ones. */
    const short = [
      { ratio: 0.382, price: 1983, y: 129 },
      { ratio: 0.5, price: 1986, y: 116 },
      { ratio: 0.618, price: 1988, y: 108 },
      { ratio: 0.786, price: 1990, y: 100 },
    ];

    render(<FibonacciOverlay fibs={short} />);

    // Topmost on screen is 78.6% here, and it is the one that survives.
    expect(screen.getByText("78.6% · 1990.00")).toBeInTheDocument();
    expect(screen.queryByText("61.8% · 1988.00")).not.toBeInTheDocument();
  });

  it("leaves a well-spread band fully labelled", () => {
    render(<FibonacciOverlay fibs={FIBS} />);

    expect(screen.getAllByText(/% · /)).toHaveLength(4);
  });
});
