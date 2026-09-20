/**
 * Engine timestamps are real UTC, and must not be shifted like MT5's.
 *
 * Two kinds of epoch reach this dashboard and they are three hours apart:
 *
 *   * **MT5 deal stamps** (`close_ts`) are broker time — UTC+3 encoded as a
 *     UTC epoch — and need `formatBrokerTime`, which shifts them back.
 *   * **Everything this app writes itself** — a Breakout signal's
 *     `created_at`, its analysis log's `ts`, the shadow ledger's `ts`, a DPM
 *     calibration's `calibrated_at` — is a plain `time.time()`, a real UTC
 *     instant. Shifting those puts every row three hours EARLY.
 *
 * Four panels did the second thing. Verified against the running app on
 * 2026-09-20: the newest Breakout signal was created 18 Sep 05:38 UTC and the
 * panel rendered 03:38.
 *
 * These render a known epoch and assert the honest reading.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ShadowSection } from "../internal/ShadowSection";
import { BreakoutActivity } from "../internal/BreakoutActivity";

// 15 Jun 2025, 16:06 UTC. In Europe/London that is 17:06 (BST), and shifted
// back three hours it is 14:06 — three distinguishable readings.
const TS = 1_750_003_600;
const UTC = "15 Jun, 16:06";
const SHIFTED = "14:06";

describe("the shadow ledger", () => {
  it("renders a decision at the time it was actually made", () => {
    render(
      <ShadowSection
        shadow={[]}
        history={[{ ts: TS, signal_ref: "RE-0001", variant: "champion",
                    would_take: 1, reason: "took it", r: 1.2 }]}
        realised={{}}
      />,
    );

    expect(screen.getByText(UTC)).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(SHIFTED))).not.toBeInTheDocument();
  });
});

describe("the breakout activity panel", () => {
  it("renders a signal at the time it was created", async () => {
    render(
      <BreakoutActivity
        signals={[{ id: 1, signal_ref: "BO-0001", direction: "BUY",
                    created_at: TS, status: "closed", outcome: "win" }]}
        log={[]}
        params={{}}
      />,
    );
    // The panels open on demand; nothing is rendered until they do.
    await userEvent.click(screen.getByTestId("bo-signals-toggle"));

    expect(screen.getByText(UTC)).toBeInTheDocument();
  });

  it("renders an analysis-log entry at the time it was written", async () => {
    render(
      <BreakoutActivity
        signals={[]}
        log={[{ ts: TS, result: "level_cooldown",
                suppressed_reason: "BUY level 3998 last fired 10min ago" }]}
        params={{}}
      />,
    );
    await userEvent.click(screen.getByTestId("bo-log-toggle"));

    expect(screen.getByText(UTC)).toBeInTheDocument();
  });
});
