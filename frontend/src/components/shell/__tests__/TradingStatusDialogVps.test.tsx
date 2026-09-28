/**
 * On a Mac that hands its orders to the VPS, the badge shows the VPS's status
 * (2026-09-28: a breaker tripped on the VPS while the Mac read "Trading
 * Active"). The dialog behind it must not then offer the Mac's own Pause or
 * Resume: both write the Mac's database, and the Mac is not placing orders,
 * so either button would report success and change nothing that trades.
 */
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { TradingStatusDialog } from "../TradingStatusDialog";
import type { TradingStatus } from "../TradingStatusDialog";

const VPS_HALTED: TradingStatus = {
  state: "halted", label: "VPS: Trading Paused until 28 Sep 14:05",
  detail: "Circuit breaker active (3 consecutive losses)",
  until: 1_758_000_000, resume_ts: null, can_resume: false, node: "vps",
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) })));
});
afterEach(() => vi.unstubAllGlobals());

const show = (status: TradingStatus) =>
  render(<TradingStatusDialog open onOpenChange={() => {}} status={status} onChanged={() => {}} />);

it("shows the VPS's state and says where to act on it", () => {
  show(VPS_HALTED);

  expect(screen.getByText(VPS_HALTED.label)).toBeInTheDocument();
  expect(screen.getByText(VPS_HALTED.detail)).toBeInTheDocument();
  expect(screen.getByTestId("trading-status-vps-note")).toHaveTextContent(/on the VPS/i);
});

it("offers neither Pause nor Resume from this node", () => {
  show(VPS_HALTED);

  expect(screen.queryByRole("button", { name: /pause now/i })).toBeNull();
  expect(screen.queryByRole("button", { name: /resume trading/i })).toBeNull();
  expect(screen.queryByLabelText(/pause for/i)).toBeNull();
});

it("does the same when the VPS reports all clear", () => {
  show({ ...VPS_HALTED, state: "ok", label: "VPS: Trading Active", detail: "", until: null });

  expect(screen.queryByRole("button", { name: /pause now/i })).toBeNull();
});
