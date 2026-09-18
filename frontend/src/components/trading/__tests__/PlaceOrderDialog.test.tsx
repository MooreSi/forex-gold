/**
 * The dialog that spends real money.
 *
 * Nothing here touches a broker: `fetch` is stubbed and the assertions are
 * about what the UI would have sent and what it showed. The rules being pinned
 * come from the frontend money conventions — confirmation names instrument,
 * direction and size; nothing destructive on a single click; a disabled
 * control shows its reason; a refusal is surfaced verbatim.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PlaceOrderDialog } from "../PlaceOrderDialog";

let fetchMock: ReturnType<typeof vi.fn>;

const ok = () => ({ ok: true, status: 200, json: async () => ({ trade_id: "T-1" }) });
const refusal = (message: string) => ({
  ok: false, status: 409,
  json: async () => ({ error: { kind: "refusal", message, ref: null } }),
});

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue(ok());
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

function open(props: Partial<React.ComponentProps<typeof PlaceOrderDialog>> = {}) {
  return render(
    <PlaceOrderDialog
      open
      onOpenChange={() => {}}
      onPlaced={() => {}}
      disabledReason={null}
      {...props}
    />,
  );
}

const sentBody = () => JSON.parse(fetchMock.mock.calls[0][1].body as string);

describe("the confirmation step", () => {
  it("does not send anything on the first click", async () => {
    // "Review" is not "Place". A destructive action on a single click is the
    // thing this two-step exists to prevent.
    open();
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("names the instrument, the direction and the size before sending", async () => {
    open();
    await userEvent.type(screen.getByLabelText(/Lots/i), "0.05");
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
    const summary = screen.getByTestId("order-summary");
    expect(summary).toHaveTextContent("BUY");
    expect(summary).toHaveTextContent("XAUUSD");
    expect(summary).toHaveTextContent("0.05 lots");
  });

  it("says where the size comes from when the field is left blank", async () => {
    open();
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
    expect(screen.getByText(/calculated from your risk settings/)).toBeInTheDocument();
    expect(screen.getByText(/stop loss calculated by DPM/)).toBeInTheDocument();
  });

  it("offers a way back that is not the confirm button", async () => {
    open();
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
    await userEvent.click(screen.getByRole("button", { name: /Back/ }));
    expect(screen.getByRole("button", { name: /Review BUY/ })).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("what gets sent", () => {
  it("posts to the market order endpoint with the direction chosen", async () => {
    open();
    await userEvent.click(screen.getByRole("button", { name: "SELL" }));
    await userEvent.click(screen.getByRole("button", { name: /Review SELL/ }));
    await userEvent.click(screen.getByRole("button", { name: /Place this SELL/ }));
    expect(fetchMock.mock.calls[0][0]).toBe("/api/trading/orders/market");
    expect(fetchMock.mock.calls[0][1].method).toBe("POST");
    expect(sentBody().direction).toBe("SELL");
  });

  it("leaves a blank stop loss as null so DPM still decides it", async () => {
    // Substituting a number here would take the stop away from the risk
    // engine without anybody noticing.
    open();
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
    await userEvent.click(screen.getByRole("button", { name: /Place this BUY/ }));
    expect(sentBody().stop_loss).toBeNull();
    expect(sentBody().lot_size).toBeNull();
  });

  it("sends the numbers that were typed", async () => {
    open();
    await userEvent.type(screen.getByLabelText(/Lots/i), "0.02");
    await userEvent.type(screen.getByLabelText(/Stop loss/i), "2450.5");
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
    await userEvent.click(screen.getByRole("button", { name: /Place this BUY/ }));
    expect(sentBody()).toMatchObject({
      direction: "BUY", lot_size: 0.02, stop_loss: 2450.5,
    });
  });
});

describe("when the backend refuses", () => {
  it("shows the backend's own words, not a generic message", async () => {
    const reason = "Trading is halted: the daily loss limit has been reached.";
    fetchMock.mockResolvedValue(refusal(reason));
    open();
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
    await userEvent.click(screen.getByRole("button", { name: /Place this BUY/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent(reason);
  });

  it("stays on the confirm step so the order is not silently lost", async () => {
    fetchMock.mockResolvedValue(refusal("no"));
    open();
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
    await userEvent.click(screen.getByRole("button", { name: /Place this BUY/ }));
    expect(await screen.findByRole("button", { name: /Place this BUY/ })).toBeInTheDocument();
  });
});

describe("when trading is not allowed", () => {
  it("disables the control AND shows the reason", async () => {
    // A greyed-out Execute button with no explanation is indistinguishable
    // from a broken one.
    const why = "Stood down as Remote — the VPS node is trading.";
    open({ disabledReason: why });
    const review = screen.getByRole("button", { name: /Review BUY/ });
    expect(review).toBeDisabled();
    expect(review).toHaveAttribute("title", why);
    expect(screen.getByRole("status")).toHaveTextContent(why);
  });

  it("is enabled when there is no reason — negative control", () => {
    open({ disabledReason: null });
    expect(screen.getByRole("button", { name: /Review BUY/ })).toBeEnabled();
    expect(screen.queryByRole("status")).toBeNull();
  });
});
