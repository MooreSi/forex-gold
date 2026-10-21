/**
 * The NEWS button on the Chart tab's Broker chart and the Dashboard's chart
 * (asked for 2026-10-21). What is tested is what each asks for and what it
 * marks; where a release lands on a bar is newsMarks.test.ts.
 */
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChartPanel } from "../ChartPanel";
import { MarketChartCard } from "@/components/dashboard/internal/MarketChartCard";
import { resetPolls } from "@/hooks/usePoll";
import { crosshairHandlers, touchedAfterRemove } from "@/test/chartStub";

vi.mock("lightweight-charts", async () => (await import("@/test/chartStub")).chartStub());

// A June bar in broker time (UTC+3), and a US release one minute into it.
const BAR = 1_750_000_000;
const CANDLES = [
  { ts: BAR - 300, open: 2425, high: 2431, low: 2424, close: 2430 },
  { ts: BAR, open: 2430, high: 2442, low: 2428, close: 2440 },
];
const RELEASE = BAR - 3 * 3600 + 60;
const EVENTS = [
  { ts: RELEASE, title: "Core CPI m/m", currency: "USD", impact: "high", forecast: "0.3%", previous: "0.2%" },
  { ts: RELEASE, title: "CPI y/y", currency: "USD", impact: "medium", forecast: "", previous: "" },
  { ts: RELEASE, title: "Euro thing", currency: "EUR", impact: "low", forecast: "", previous: "" },
];

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  resetPolls();
  try { localStorage.clear(); } catch { /* jsdom here has none */ }
  touchedAfterRemove.length = 0;
  crosshairHandlers.length = 0;
  fetchMock = vi.fn(async (url: string) => {
    const body = url.startsWith("/api/chart/candles") ? CANDLES
      : url.startsWith("/api/chart/overlays") ? { emas: {}, rsi: [], fvgs: [] }
      : url.startsWith("/api/calendar/chart_news") ? { events: EVENTS }
      : [];
    return { ok: true, status: 200, json: async () => body };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const asked = (prefix: string) =>
  fetchMock.mock.calls.map((c) => String(c[0])).filter((u) => u.startsWith(prefix));

describe("the Broker chart's NEWS button", () => {
  it("asks for nothing until it is pressed", async () => {
    render(<ChartPanel />);
    await screen.findByTestId("candle-chart");

    expect(asked("/api/calendar/chart_news")).toEqual([]);
    expect(screen.queryByTestId("news-layer")).toBeNull();
  });

  it("marks the bar a release fell in, by its strongest impact", async () => {
    render(<ChartPanel />);
    await screen.findByTestId("candle-chart");

    await userEvent.click(screen.getByRole("button", { name: /NEWS/ }));

    expect(await screen.findByTestId(`news-mark-high-${BAR}`)).toBeInTheDocument();
    const url = asked("/api/calendar/chart_news")[0];
    expect(url).toMatch(/from_ts=\d+&to_ts=\d+/);
  });

  it("offers high, medium and low, and drops a level that is switched off", async () => {
    render(<ChartPanel />);
    await screen.findByTestId("candle-chart");
    await userEvent.click(screen.getByRole("button", { name: /NEWS/ }));
    await screen.findByTestId(`news-mark-high-${BAR}`);

    await userEvent.click(screen.getByRole("button", { name: "high impact" }));

    expect(await screen.findByTestId(`news-mark-medium-${BAR}`)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "low impact" })).toBeInTheDocument();
  });

  it("shows the releases and the move after them on hover", async () => {
    render(<ChartPanel />);
    await screen.findByTestId("candle-chart");
    await userEvent.click(screen.getByRole("button", { name: /NEWS/ }));
    await screen.findByTestId(`news-mark-high-${BAR}`);

    act(() => crosshairHandlers.forEach((h) => h({ time: BAR })));

    const hover = await screen.findByTestId("news-hover");
    expect(hover).toHaveTextContent("Core CPI m/m");
    expect(hover).toHaveTextContent("f 0.3%");
    // USD and gold only by default: the euro release is not listed.
    expect(hover).not.toHaveTextContent("Euro thing");
    // From the bar's open (2430) to the last close loaded (2440).
    expect(hover).toHaveTextContent("+10.00");
  });

  it("lists every currency when asked", async () => {
    render(<ChartPanel />);
    await screen.findByTestId("candle-chart");
    await userEvent.click(screen.getByRole("button", { name: /NEWS/ }));
    await screen.findByTestId(`news-mark-high-${BAR}`);

    await userEvent.click(screen.getByRole("button", { name: "Currencies" }));
    act(() => crosshairHandlers.forEach((h) => h({ time: BAR })));

    expect(await screen.findByTestId("news-hover")).toHaveTextContent("Euro thing");
  });

  it("leaves nothing behind on the chart after unmounting", async () => {
    const { unmount } = render(<ChartPanel />);
    await screen.findByTestId("candle-chart");
    await userEvent.click(screen.getByRole("button", { name: /NEWS/ }));
    await screen.findByTestId(`news-mark-high-${BAR}`);

    unmount();

    expect(touchedAfterRemove).toEqual([]);
  });
});

describe("the Dashboard chart's NEWS button", () => {
  it("marks the same release", async () => {
    render(
      <MarketChartCard
        candles={CANDLES} overlays={null} tick={null} trades={[]}
        timeframe="5m" onTimeframe={() => {}} error={null}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: /NEWS/ }));

    await waitFor(() => expect(asked("/api/calendar/chart_news")).toHaveLength(1));
    expect(await screen.findByTestId(`news-mark-high-${BAR}`)).toBeInTheDocument();
  });
});
