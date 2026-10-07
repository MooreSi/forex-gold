/**
 * Scroll-back on the Broker chart (docs/todo/011 phase 2).
 *
 * Panning to the left edge asks for the bars before the oldest one held, once
 * per edge; the answer is prepended without a duplicate or out-of-order bar.
 * What is asserted is what the chart ASKS FOR and what it hands the series --
 * the canvas is the stub's.
 */
import { act, render, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChartPanel } from "../ChartPanel";
import { mergeHistory } from "../internal/history";
import { resetPolls } from "@/hooks/usePoll";
import { logicalRangeHandlers, touchedAfterRemove } from "@/test/chartStub";

vi.mock("lightweight-charts", async () => (await import("@/test/chartStub")).chartStub());

const bar = (ts: number) => ({ ts, open: 1, high: 2, low: 0.5, close: 1.5 });
const LIVE = [bar(1_750_000_000), bar(1_750_000_300), bar(1_750_000_600)];

let fetchMock: ReturnType<typeof vi.fn>;
let historyBody: unknown[];

beforeEach(() => {
  resetPolls();
  touchedAfterRemove.length = 0;
  logicalRangeHandlers.length = 0;
  historyBody = [bar(1_749_999_400), bar(1_749_999_700)];
  fetchMock = vi.fn(async (url: string) => {
    if (url.startsWith("/api/chart/candles")) {
      return { ok: true, status: 200, json: async () => LIVE };
    }
    if (url.startsWith("/api/chart/history")) {
      return { ok: true, status: 200, json: async () => historyBody };
    }
    if (url.startsWith("/api/chart/overlays")) {
      return { ok: true, status: 200, json: async () => ({ emas: {}, rsi: [], fvgs: [] }) };
    }
    return { ok: true, status: 200, json: async () => [] };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const asked = (prefix: string) =>
  fetchMock.mock.calls.map((c) => String(c[0])).filter((u) => u.startsWith(prefix));

const pan = (from: number, to: number) =>
  act(() => { for (const fn of [...logicalRangeHandlers]) fn({ from, to }); });

async function opened() {
  render(<ChartPanel />);
  await waitFor(() => expect(logicalRangeHandlers.length).toBeGreaterThan(0));
  // Candles arrive asynchronously; the edge is measured against them.
  await waitFor(() => expect(asked("/api/chart/candles").length).toBeGreaterThan(0));
  await act(async () => { await Promise.resolve(); });
}

describe("mergeHistory", () => {
  it("puts older bars first and keeps every live bar", () => {
    const out = mergeHistory([bar(100), bar(200)], [bar(300), bar(400)]);
    expect(out.map((b) => b.ts)).toEqual([100, 200, 300, 400]);
  });

  it("drops an older bar that overlaps the live window rather than drawing it twice", () => {
    const out = mergeHistory([bar(100), bar(300), bar(350)], [bar(300), bar(400)]);
    expect(out.map((b) => b.ts)).toEqual([100, 300, 400]);
  });

  it("orders and de-duplicates history pages that arrived out of order", () => {
    const out = mergeHistory([bar(200), bar(100), bar(200)], [bar(300)]);
    expect(out.map((b) => b.ts)).toEqual([100, 200, 300]);
  });

  it("with no history is the live window unchanged", () => {
    expect(mergeHistory([], LIVE)).toEqual(LIVE);
  });
});

describe("panning to the left edge", () => {
  it("asks once for the bars before the oldest one held", async () => {
    await opened();
    pan(1, 60);
    pan(0, 59);   // still at the edge while the first request is out
    await waitFor(() => expect(asked("/api/chart/history").length).toBe(1));
    const url = asked("/api/chart/history")[0];
    expect(url).toContain("timeframe=5m");
    expect(url).toContain(`before=${LIVE[0].ts}`);
  });

  it("asks nothing while the view is away from the edge", async () => {
    await opened();
    pan(40, 100);
    await act(async () => { await Promise.resolve(); });
    expect(asked("/api/chart/history")).toEqual([]);
  });

  it("asks from the new oldest bar on the next visit to the edge", async () => {
    await opened();
    pan(0, 60);
    await waitFor(() => expect(asked("/api/chart/history").length).toBe(1));
    await act(async () => { await Promise.resolve(); });
    pan(0, 60);
    await waitFor(() => expect(asked("/api/chart/history").length).toBe(2));
    expect(asked("/api/chart/history")[1]).toContain(`before=${1_749_999_400}`);
  });

  it("stops asking once the broker has nothing older", async () => {
    historyBody = [];
    await opened();
    pan(0, 60);
    await waitFor(() => expect(asked("/api/chart/history").length).toBe(1));
    await act(async () => { await Promise.resolve(); });
    pan(0, 60);
    await act(async () => { await Promise.resolve(); });
    expect(asked("/api/chart/history").length).toBe(1);
  });

  it("starts over on a new timeframe", async () => {
    await opened();
    pan(0, 60);
    await waitFor(() => expect(asked("/api/chart/history").length).toBe(1));
    await userEvent.click(screen.getByRole("button", { name: "1H" }));
    await waitFor(() =>
      expect(asked("/api/chart/candles").some((u) => u.includes("timeframe=1H"))).toBe(true));
    await act(async () => { await Promise.resolve(); });
    pan(0, 60);
    await waitFor(() =>
      expect(asked("/api/chart/history").some((u) => u.includes("timeframe=1H"))).toBe(true));
    const last = asked("/api/chart/history").at(-1)!;
    expect(last).toContain(`before=${LIVE[0].ts}`);
  });
});
