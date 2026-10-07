/**
 * The Strategy choice on the Market and Limit order dialogs (owner,
 * 2026-10-07): "select a strategy/ea or leave it blank and just use the take
 * profit figure which would be a 100% take profit", and the limit order "the
 * same options instead of multiple tp levels".
 *
 * Blank is sent as `orb_fixed`, the single full close at TP. Sending null
 * instead is what used to drop a plain market order into the global Scale Out
 * + Breakeven. Nothing here touches a broker: `fetch` is stubbed.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PlaceLimitOrderDialog } from "../PlaceLimitOrderDialog";
import { PlaceOrderDialog } from "../PlaceOrderDialog";
import { strategyChoices } from "../internal/orderStrategy";

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue({
    ok: true, status: 200, json: async () => ({ trade_id: "T-1" }),
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const sentBody = () => JSON.parse(fetchMock.mock.calls[0][1].body as string);

const CHOICES = [
  { key: "scale_out", label: "Scale Out + Breakeven" },
  { key: "template:GD single", label: "Template: GD single" },
];

function market() {
  render(<PlaceOrderDialog open onOpenChange={() => {}} onPlaced={() => {}}
    disabledReason={null} strategies={CHOICES} />);
}

function limit() {
  render(<PlaceLimitOrderDialog open onOpenChange={() => {}} onPlaced={() => {}}
    disabledReason={null} strategies={CHOICES} />);
}

async function placeMarket() {
  await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
  await userEvent.click(screen.getByRole("button", { name: /Place this BUY/ }));
}

describe("the market order", () => {
  it("starts with no strategy, and sends that as a single take profit", async () => {
    market();
    expect(screen.getByLabelText("Strategy")).toHaveValue("");
    await userEvent.type(screen.getByLabelText(/^Take profit/), "2450");
    await placeMarket();
    expect(sentBody()).toMatchObject({ strategy: "orb_fixed", take_profit: 2450 });
  });

  it("sends the strategy chosen", async () => {
    market();
    await userEvent.selectOptions(screen.getByLabelText("Strategy"), "scale_out");
    await placeMarket();
    expect(sentBody().strategy).toBe("scale_out");
  });

  it("offers EA templates", async () => {
    market();
    await userEvent.selectOptions(screen.getByLabelText("Strategy"), "template:GD single");
    await placeMarket();
    expect(sentBody().strategy).toBe("template:GD single");
  });

  it("says on the confirm step how the position will be managed", async () => {
    market();
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
    expect(screen.getByTestId("order-management")).toHaveTextContent(
      "closes at the take profit");
    await userEvent.click(screen.getByRole("button", { name: /Back/ }));
    await userEvent.selectOptions(screen.getByLabelText("Strategy"), "scale_out");
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
    expect(screen.getByTestId("order-management")).toHaveTextContent(
      "Managed by Scale Out + Breakeven");
  });
});

describe("the limit order", () => {
  async function fillAndPlace() {
    await userEvent.type(screen.getByLabelText("Entry zone low"), "2400");
    await userEvent.type(screen.getByLabelText("Entry zone high"), "2405");
    await userEvent.type(screen.getByLabelText("Stop loss"), "2390");
    await userEvent.type(screen.getByLabelText("Take profit"), "2440");
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
    await userEvent.click(screen.getByRole("button", { name: /Place this BUY/ }));
  }

  it("has one take profit, not eight", () => {
    limit();
    expect(screen.getByLabelText("Take profit")).toBeInTheDocument();
    expect(screen.queryByLabelText("TP2")).toBeNull();
  });

  it("starts with no strategy, and sends that as a single take profit", async () => {
    limit();
    await fillAndPlace();
    expect(sentBody()).toMatchObject({ strategy: "orb_fixed", tp1: 2440, tp2: null });
  });

  it("sends the strategy chosen", async () => {
    limit();
    await userEvent.selectOptions(screen.getByLabelText("Strategy"), "scale_out");
    await fillAndPlace();
    expect(sentBody().strategy).toBe("scale_out");
  });

  it("offers EA templates, as the market order does", async () => {
    limit();
    await userEvent.selectOptions(screen.getByLabelText("Strategy"), "template:GD single");
    await fillAndPlace();
    expect(sentBody().strategy).toBe("template:GD single");
  });
});

describe("what each dialog offers", () => {
  const catalogue = [
    { key: "scale_out", label: "Scale Out + Breakeven", kind: "builtin" },
    { key: "orb_fixed", label: "ORB/IVB Fixed", kind: "builtin" },
    { key: "cs-1", label: "My variant", kind: "custom" },
    { key: "template:GD single", label: "Template: GD single", kind: "template" },
  ];

  it("lists single take profit once, as blank, not again as ORB/IVB Fixed", () => {
    const keys = strategyChoices(catalogue).map((c) => c.key);
    expect(keys).toEqual(["scale_out", "template:GD single"]);
  });
});
