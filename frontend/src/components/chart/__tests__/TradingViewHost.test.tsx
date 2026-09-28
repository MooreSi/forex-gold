/**
 * The page-wide TradingView widget's box.
 *
 * Owner, 2026-09-28: "first time i go into the chart page the tradingview
 * chart is over sized, if i click broker and then back to trading view it
 * resizes correctly". TradingView's embed script, once it loads, sets its
 * container to width and height 100%. The container was the host we
 * position with `position: fixed` and pixel sizes, and 100% of a fixed box is
 * the whole window. Flipping views re-applied our pixels, which is why that
 * "fixed" it. The script now gets a box of its own inside the host.
 */
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it } from "vitest";
import { resetTradingViewHost, TradingViewChart } from "../internal/TradingViewChart";

beforeEach(() => resetTradingViewHost());
afterEach(() => resetTradingViewHost());

/** What TradingView's script does to its parent once it has loaded. */
function whatTheScriptDoes() {
  const script = document.querySelector<HTMLScriptElement>('script[src*="tradingview.com"]')!;
  const parent = script.parentElement!;
  parent.style.width = "100%";
  parent.style.height = "100%";
}

it("keeps the chart slot's size when TradingView's script sizes its container", () => {
  render(<TradingViewChart />);
  const host = screen.getByTestId("tradingview-host");
  const before = { width: host.style.width, height: host.style.height };

  whatTheScriptDoes();

  expect(host.style.width).toBe(before.width);
  expect(host.style.height).toBe(before.height);
  expect(host.style.width).toMatch(/px$/);
});

it("gives the script a box that fills the host", () => {
  render(<TradingViewChart />);
  const script = document.querySelector('script[src*="tradingview.com"]')!;
  const box = script.parentElement!;

  expect(box).not.toBe(screen.getByTestId("tradingview-host"));
  expect(box.parentElement).toBe(screen.getByTestId("tradingview-host"));
  expect(box.className).toContain("tradingview-widget-container");
});
