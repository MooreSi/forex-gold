import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EquityCurveSection } from "../internal/EquityCurveSection";
import { resetPolls } from "@/hooks/usePoll";

/**
 * The realised-P&L curve for the window.
 *
 * It is NOT account equity and the panel says so: starting it at the account
 * balance would mean inventing where the account stood when the window
 * opened, and a curve whose zero is a guess can be read as a loss when the
 * account never moved.
 */
const CURVE = {
  points: [
    { ts: 1757000000, pnl: 0 },
    { ts: 1757000000, pnl: 80 },
    { ts: 1757100000, pnl: 30 },
    { ts: 1757200000, pnl: 120 },
  ],
  net: 120, peak: 120, max_drawdown: 50, trades: 3,
};

let body: unknown;

beforeEach(() => {
  resetPolls();
  body = { rows: [], error: null, curve: CURVE };
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true, status: 200, json: async () => body,
  })));
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

describe("EquityCurveSection", () => {
  it("draws a path once there are points", async () => {
    render(<EquityCurveSection days={30} />);

    expect(await screen.findByTestId("equity-path")).toBeInTheDocument();
  });

  it("reports the net, the peak and the worst drawdown", async () => {
    render(<EquityCurveSection days={30} />);

    expect(await screen.findByTestId("curve-net")).toHaveTextContent("$120.00");
    expect(screen.getByTestId("curve-peak")).toHaveTextContent("$120.00");
    expect(screen.getByTestId("curve-drawdown")).toHaveTextContent("$50.00");
  });

  it("colours a losing window red", async () => {
    body = { rows: [], error: null, curve: { ...CURVE, net: -430.5 } };
    render(<EquityCurveSection days={30} />);

    expect(await screen.findByTestId("curve-net")).toHaveClass(/text-loss/);
  });

  it("says what the curve measures, so it is not read as the account", async () => {
    render(<EquityCurveSection days={30} />);
    await screen.findByTestId("equity-path");

    expect(screen.getByText(/not the account balance/i)).toBeInTheDocument();
  });

  it("says a quiet window is quiet rather than drawing a flat line", async () => {
    // A flat line reads as "traded all month and broke even".
    body = { rows: [], error: null,
             curve: { points: [], net: 0, peak: 0, max_drawdown: 0, trades: 0 } };
    render(<EquityCurveSection days={30} />);

    expect(await screen.findByText(/No closed trades/i)).toBeInTheDocument();
    expect(screen.queryByTestId("equity-path")).not.toBeInTheDocument();
  });

  it("shows the broker's refusal rather than an empty chart", async () => {
    body = { rows: [], error: "MT5 deal history is unavailable.",
             curve: { points: [], net: 0, peak: 0, max_drawdown: 0, trades: 0 } };
    render(<EquityCurveSection days={30} />);

    expect(await screen.findByText(/MT5 deal history is unavailable/)).toBeInTheDocument();
  });

  it("survives a payload with no curve at all", async () => {
    // A half-deployed backend answers the old shape. Reading `.points` off
    // undefined throws inside render and takes the dashboard with it.
    body = { rows: [], error: null };
    render(<EquityCurveSection days={30} />);

    expect(await screen.findByText(/No closed trades/i)).toBeInTheDocument();
  });

  it("counts the trades the curve was drawn from", async () => {
    render(<EquityCurveSection days={30} />);

    expect(await screen.findByTestId("curve-trades")).toHaveTextContent("3");
  });
});

describe("reading a value off it", () => {
  /**
   * Owner, 2026-09-21: "the equity curve needs to look prettier like the
   * original forex app and is missing the values/data also use a gradient
   * colour scheme like the original app".
   *
   * The port drew a bare line on a bare box: no axis anywhere, so a curve
   * that ended at +$120 was indistinguishable from one that ended at
   * +$12,000, and nothing said when any of it happened. The NiceGUI chart it
   * replaced had a right-hand money axis, dated category labels, dashed
   * gridlines, a gradient area under the line and a tooltip. These are those.
   */
  it("labels the money axis, so the height of the line means something", async () => {
    render(<EquityCurveSection days={30} />);
    await screen.findByTestId("equity-path");

    const labels = screen.getAllByTestId(/^curve-y-/).map((n) => n.textContent);
    expect(labels.length).toBeGreaterThanOrEqual(3);
    expect(labels.join(" ")).toMatch(/\$/);
  });

  it("always labels zero, because that is what the curve is measured from", async () => {
    render(<EquityCurveSection days={30} />);
    await screen.findByTestId("equity-path");

    expect(screen.getAllByTestId(/^curve-y-/).map((n) => n.textContent))
      .toContain("$0");
  });

  it("dates the ends, so the window is not a mystery", async () => {
    render(<EquityCurveSection days={30} />);
    await screen.findByTestId("equity-path");

    expect(screen.getAllByTestId(/^curve-x-/).length).toBeGreaterThanOrEqual(2);
  });

  it("fills under the line with a gradient, not a flat block", async () => {
    const { container } = render(<EquityCurveSection days={30} />);
    await screen.findByTestId("equity-path");

    const area = screen.getByTestId("equity-area");
    expect(area.getAttribute("fill")).toMatch(/^url\(#/);
    expect(container.querySelector("linearGradient")).toBeInTheDocument();
  });

  it("closes the area back to the zero line, not to the bottom of the box", async () => {
    // Filled to the floor, a window that lost money shades the same way a
    // window that made money does.
    render(<EquityCurveSection days={30} />);
    await screen.findByTestId("equity-path");

    const line = screen.getByTestId("equity-path").getAttribute("d") ?? "";
    const area = screen.getByTestId("equity-area").getAttribute("d") ?? "";
    expect(area.startsWith(line)).toBe(true);
  });

  it("marks where it ended and says the figure", async () => {
    render(<EquityCurveSection days={30} />);

    expect(await screen.findByTestId("curve-last")).toHaveTextContent("$120.00");
  });

  it("says a losing window in the loss colour, gradient and all", async () => {
    body = { rows: [], error: null,
             curve: { ...CURVE, points: [{ ts: 1757000000, pnl: 0 },
                                         { ts: 1757200000, pnl: -430.5 }],
                      net: -430.5, peak: 0, max_drawdown: 430.5 } };
    render(<EquityCurveSection days={30} />);
    await screen.findByTestId("equity-path");

    expect(screen.getByTestId("equity-path").getAttribute("class"))
      .toMatch(/text-loss/);
  });
});
