import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RiskSection } from "../internal/RiskSection";
import { RISK_KEYS } from "../content/risk";

/**
 * The numbers that decide how much money a trade can lose.
 *
 * MOVED here from Settings on 2026-09-19, at the owner's request and with the
 * tests that were guarding it — these assertions were written after the first
 * React version of this screen offered four fields out of twenty-two, two of
 * which named columns that do not exist, so they raised on save and the value
 * was silently lost. None of that is less true for being on another tab.
 *
 * The endpoint did not move: `/api/settings/risk` is still where these live.
 */
const BODIES: Record<string, unknown> = {
  "/api/settings/risk": {
    risk_per_trade_pct: 1, max_risk_per_trade_pct: 2, max_open_trades: 3,
    max_lot_size: 0.5, max_daily_loss_pct: 5, max_total_drawdown_pct: 10,
    giveback_guard_enabled: 0, giveback_arm_usd: 50, giveback_pct: 40,
    circuit_breaker_enabled: 1, circuit_breaker_losses: 3,
    circuit_breaker_cooldown_mins: 60, internal_hedge_mode: "off",
    internal_net_exposure_max_lots: 0.5, dpm_enabled: 0, profit_close_usd: 0,
    risk_governor_enabled: 0,
  },
};

let fetchMock: ReturnType<typeof vi.fn>;
let overrides: Record<string, unknown>;

beforeEach(() => {
  overrides = {};
  fetchMock = vi.fn(async (url: string) => {
    const key = String(url);
    const body = (overrides[key] ?? BODIES[key] ?? {}) as Record<string, unknown>;
    const status = Number(body.__status ?? 200);
    return { ok: status < 400, status, json: async () => body };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const writes = () => fetchMock.mock.calls.filter((c) => c[1]?.method && c[1].method !== "GET");

describe("risk", () => {
  it("shows the stored numbers, under the names the column actually has", async () => {
    // The first React version of this tab offered `risk_pct` and
    // `daily_loss_limit_pct`. Neither is a column of vantage_risk_settings, so
    // both raised on save and the value was silently lost while the box kept
    // showing what was typed.
    render(<RiskSection />);

    expect(await screen.findByLabelText("Risk per trade (%)")).toHaveValue("1");
    expect(screen.getByLabelText("Maximum open trades")).toHaveValue("3");
    expect(screen.getByLabelText("Max daily loss (%)")).toHaveValue("5");
  });

  it("saves a field when it is left, not on every keystroke", async () => {
    render(<RiskSection />);
    const field = await screen.findByLabelText("Risk per trade (%)");

    await userEvent.clear(field);
    await userEvent.type(field, "2");
    expect(writes()).toHaveLength(0);

    await userEvent.tab();

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({ risk_per_trade_pct: 2 });
  });

  it("adopts the value the backend stored, not the one that was typed", async () => {
    // The risk service clamps. A field that kept the operator's number would
    // tell them the engine is using a value it is not.
    overrides["/api/settings/risk"] = {
      ...BODIES["/api/settings/risk"] as object, risk_per_trade_pct: 2,
    };
    render(<RiskSection />);
    const field = await screen.findByLabelText("Risk per trade (%)");

    await userEvent.clear(field);
    await userEvent.type(field, "99");
    await userEvent.tab();

    await waitFor(() => expect(field).toHaveValue("2"));
  });

  it("says these decide how much a trade can lose", async () => {
    render(<RiskSection />);

    expect(await screen.findByText(/how much a single trade can lose/)).toBeInTheDocument();
  });

  it("offers every setting the store has a column for, not four of them", async () => {
    // The tab shipped with 4 fields where the NiceGUI page had 22. An operator
    // cannot set a give-back limit, a circuit breaker or an exposure cap from a
    // screen that does not show them.
    render(<RiskSection />);
    await screen.findByLabelText("Risk per trade (%)");

    for (const key of RISK_KEYS) {
      expect(screen.getByTestId(`risk-${key}`)).toBeInTheDocument();
    }
    expect(RISK_KEYS.length).toBeGreaterThanOrEqual(20);
  });

  it("saves a toggle as the 0/1 the column holds", async () => {
    render(<RiskSection />);

    await userEvent.click(await screen.findByLabelText("Risk governor"));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({ risk_governor_enabled: 1 });
  });

  it("saves the hedging mode as the string the column holds", async () => {
    render(<RiskSection />);

    await userEvent.selectOptions(await screen.findByLabelText("Hedging"), "net_exposure");

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({ internal_hedge_mode: "net_exposure" });
  });

  it("says when a setting does nothing because its switch is off", async () => {
    // "I turned it on and it made no difference" is how a setting gets
    // reported as broken.
    render(<RiskSection />);
    await screen.findByLabelText("Risk per trade (%)");

    expect(within(screen.getByTestId("risk-giveback_arm_usd"))
      .getByText(/Does nothing until the give-back guard is on/)).toBeInTheDocument();
  });

  it("stops saying so once the switch is on", async () => {
    // Negative control: a note shown always is a note nobody reads.
    overrides["/api/settings/risk"] = {
      ...BODIES["/api/settings/risk"] as object, giveback_guard_enabled: 1,
    };
    render(<RiskSection />);
    await screen.findByLabelText("Risk per trade (%)");

    expect(within(screen.getByTestId("risk-giveback_arm_usd"))
      .queryByText(/Does nothing until/)).not.toBeInTheDocument();
  });
});
