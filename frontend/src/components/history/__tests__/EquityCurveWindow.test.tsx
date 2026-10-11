import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EquityCurveSection } from "../internal/EquityCurveSection";
import { resetPolls } from "@/hooks/usePoll";

/**
 * Owner, 2026-10-09: "the chart needs to be wider, add a slider so you can
 * change the time window of the equity curve, also make it look more
 * professional".
 *
 * The slider narrows what is drawn within the window already loaded (the
 * 7/30/90/365-day buttons decide that). Every figure above the chart follows
 * the view, so the numbers and the line never disagree.
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

beforeEach(() => {
  resetPolls();
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true, status: 200, json: async () => ({ rows: [], error: null, curve: CURVE }),
  })));
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

describe("the time-window slider", () => {
  it("offers two handles spanning the whole window by default", async () => {
    render(<EquityCurveSection days={30} />);
    await screen.findByTestId("equity-path");

    const from = screen.getByLabelText(/window start/i) as HTMLInputElement;
    const to = screen.getByLabelText(/window end/i) as HTMLInputElement;
    expect(Number(from.value)).toBe(1757000000);
    expect(Number(to.value)).toBe(1757200000);
  });

  it("re-measures every figure over the narrowed view", async () => {
    render(<EquityCurveSection days={30} />);
    await screen.findByTestId("equity-path");

    // From just after the first trade: the trades at 1757100000 (-50) and
    // 1757200000 (+90), starting from where the first one left the account.
    fireEvent.change(screen.getByLabelText(/window start/i),
      { target: { value: "1757050000" } });

    expect(screen.getByTestId("curve-net")).toHaveTextContent("$40.00");
    expect(screen.getByTestId("curve-trades")).toHaveTextContent("2");
    expect(screen.getByTestId("curve-drawdown")).toHaveTextContent("$50.00");
  });

  it("keeps the start before the end", async () => {
    render(<EquityCurveSection days={30} />);
    await screen.findByTestId("equity-path");

    fireEvent.change(screen.getByLabelText(/window end/i),
      { target: { value: "1757100000" } });
    fireEvent.change(screen.getByLabelText(/window start/i),
      { target: { value: "1757200000" } });

    const from = Number((screen.getByLabelText(/window start/i) as HTMLInputElement).value);
    const to = Number((screen.getByLabelText(/window end/i) as HTMLInputElement).value);
    expect(from).toBeLessThanOrEqual(to);
  });

  it("says a window with no trades in it is empty instead of drawing nothing", async () => {
    render(<EquityCurveSection days={30} />);
    await screen.findByTestId("equity-path");

    fireEvent.change(screen.getByLabelText(/window start/i),
      { target: { value: "1757110000" } });
    fireEvent.change(screen.getByLabelText(/window end/i),
      { target: { value: "1757190000" } });

    expect(screen.getByText(/no trades closed between/i)).toBeInTheDocument();
    // The slider stays, so the operator can widen it again.
    expect(screen.getByLabelText(/window start/i)).toBeInTheDocument();
  });

  it("puts the whole window back with one click", async () => {
    render(<EquityCurveSection days={30} />);
    await screen.findByTestId("equity-path");

    fireEvent.change(screen.getByLabelText(/window start/i),
      { target: { value: "1757050000" } });
    fireEvent.click(screen.getByRole("button", { name: /^all$/i }));

    expect(screen.getByTestId("curve-net")).toHaveTextContent("$120.00");
    expect(screen.getByTestId("curve-trades")).toHaveTextContent("3");
  });
});

describe("reading a point off it", () => {
  it("shows the time, the running total and that trade's result on hover", async () => {
    render(<EquityCurveSection days={30} />);
    const path = await screen.findByTestId("equity-path");
    const svg = path.closest("svg")!;
    const vb = svg.getAttribute("viewBox")!.split(" ").map(Number);
    // jsdom lays nothing out; give the chart the size its viewBox says.
    svg.getBoundingClientRect = () => ({
      left: 0, top: 0, width: vb[2], height: vb[3], right: vb[2], bottom: vb[3],
      x: 0, y: 0, toJSON: () => ({}),
    }) as DOMRect;

    // The far right: the last trade, +90 that took the total to 120.
    fireEvent.mouseMove(svg, { clientX: vb[2] - 1, clientY: 10 });

    const tip = screen.getByTestId("curve-tooltip");
    expect(tip).toHaveTextContent("$120.00");
    expect(tip).toHaveTextContent("+$90.00");

    fireEvent.mouseLeave(svg);
    expect(screen.queryByTestId("curve-tooltip")).not.toBeInTheDocument();
  });

  it("draws a drawdown strip under the curve, measured from the running peak", async () => {
    render(<EquityCurveSection days={30} />);
    await screen.findByTestId("equity-path");

    expect(screen.getByTestId("drawdown-area").getAttribute("d")).toMatch(/^M/);
  });
});
