import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RiskSection } from "../internal/RiskSection";

/**
 * Risk % OR fixed lots, never both, and the switch that applies it to EA
 * templates. docs/todo/risk/010.
 *
 * On 2026-09-24 this tab showed "Risk per trade 2%" while a fixed lot of 0.1
 * that the tab did not display sized every plain channel, and EA templates
 * sized themselves. The operator could not tell which number was in use.
 */
const BASE: Record<string, unknown> = {
  risk_per_trade_pct: 2, max_risk_per_trade_pct: 2, max_open_trades: 3,
  max_lot_size: 0.5, strategy_lot_size: 0, strategy_lot_size_parked: 0,
  global_sizing_override: 0,
};

let fetchMock: ReturnType<typeof vi.fn>;
let stored: Record<string, unknown>;

beforeEach(() => {
  stored = { ...BASE };
  // The backend echoes the row it stored, so the screen shows what the engine uses.
  fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "PUT") stored = { ...stored, ...JSON.parse(String(init.body)) };
    return { ok: true, status: 200, json: async () => stored };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const writes = () => fetchMock.mock.calls
  .filter((c) => c[1]?.method === "PUT")
  .map((c) => JSON.parse(String(c[1].body)));

describe("one or the other", () => {
  it("in risk mode, the percentage is live and the lot size is greyed out", async () => {
    render(<RiskSection />);

    expect(await screen.findByLabelText("Risk per trade (%)")).toBeEnabled();
    expect(screen.getByLabelText("Fixed lot size")).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Risk %" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("sizing-summary")).toHaveTextContent("risks 2.00%");
  });

  it("in lots mode, the reverse", async () => {
    stored = { ...BASE, strategy_lot_size: 0.1 };
    render(<RiskSection />);

    expect(await screen.findByLabelText("Fixed lot size")).toBeEnabled();
    expect(screen.getByLabelText("Fixed lot size")).toHaveValue("0.1");
    expect(screen.getByLabelText("Risk per trade (%)")).toBeDisabled();
    expect(screen.getByTestId("sizing-summary")).toHaveTextContent("opens 0.10 lots");
  });

  it("choosing Risk % stores no fixed lot and keeps the old one for later", async () => {
    stored = { ...BASE, strategy_lot_size: 0.1 };
    render(<RiskSection />);

    await userEvent.click(await screen.findByRole("radio", { name: "Risk %" }));

    await waitFor(() => expect(writes()).toEqual([
      { strategy_lot_size: 0, strategy_lot_size_parked: 0.1 },
    ]));
    // Greyed out, but still showing the lot it will come back to.
    await waitFor(() => expect(screen.getByLabelText("Fixed lot size")).toBeDisabled());
    expect(screen.getByLabelText("Fixed lot size")).toHaveValue("0.1");
  });

  it("choosing Fixed lots brings the kept lot back", async () => {
    stored = { ...BASE, strategy_lot_size_parked: 0.07 };
    render(<RiskSection />);

    await userEvent.click(await screen.findByRole("radio", { name: "Fixed lots" }));

    await waitFor(() => expect(writes()).toEqual([{ strategy_lot_size: 0.07 }]));
  });

  it("with nothing kept, it starts at the smallest lot the broker takes", async () => {
    render(<RiskSection />);

    await userEvent.click(await screen.findByRole("radio", { name: "Fixed lots" }));

    await waitFor(() => expect(writes()).toEqual([{ strategy_lot_size: 0.01 }]));
  });

  it("clicking the mode already in use saves nothing", async () => {
    render(<RiskSection />);

    await userEvent.click(await screen.findByRole("radio", { name: "Risk %" }));

    expect(writes()).toEqual([]);
  });
});

describe("the EA template override", () => {
  it("off: says templates keep their own lot size", async () => {
    render(<RiskSection />);

    expect(await screen.findByTestId("sizing-template-line"))
      .toHaveTextContent("use the lot size set on their template");
  });

  it("on: says templates follow this size, split across grid legs in risk mode", async () => {
    stored = { ...BASE, global_sizing_override: 1 };
    render(<RiskSection />);

    const line = await screen.findByTestId("sizing-template-line");
    expect(line).toHaveTextContent("own lot sizes are ignored");
    expect(line).toHaveTextContent("split across its legs");
  });

  it("saves as the 0/1 the column holds", async () => {
    render(<RiskSection />);

    await userEvent.click(await screen.findByLabelText("EA template override"));

    await waitFor(() => expect(writes()).toEqual([{ global_sizing_override: 1 }]));
  });

  it("says ORB and Set & Forget are not affected", async () => {
    render(<RiskSection />);

    expect(within(await screen.findByTestId("risk-global_sizing_override"))
      .getByText(/ORB and Set & Forget keep their own sizing/)).toBeInTheDocument();
  });
});

describe("a maximum lot size of 0", () => {
  it("is called out, because every trade is refused while it holds", async () => {
    stored = { ...BASE, max_lot_size: 0 };
    render(<RiskSection />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Every trade sizes to 0 lots");
  });

  it("is not called out when it is set", async () => {
    render(<RiskSection />);
    await screen.findByLabelText("Risk per trade (%)");

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
