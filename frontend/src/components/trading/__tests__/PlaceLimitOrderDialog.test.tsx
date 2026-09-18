/**
 * The resting limit order. **This spends real money** — later, and without
 * anybody watching, which is the difference from the market order and the
 * thing the confirmation has to say.
 *
 * Nothing here touches a broker: `fetch` is stubbed and the assertions are
 * about what would have been sent.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PlaceLimitOrderDialog } from "../PlaceLimitOrderDialog";

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue({
    ok: true, status: 200, json: async () => ({ trade_id: "T-1" }),
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

function open(props: Partial<React.ComponentProps<typeof PlaceLimitOrderDialog>> = {}) {
  return render(
    <PlaceLimitOrderDialog
      open
      onOpenChange={() => {}}
      onPlaced={() => {}}
      disabledReason={null}
      {...props}
    />,
  );
}

const fill = async () => {
  await userEvent.type(screen.getByLabelText("Entry zone low"), "2430");
  await userEvent.type(screen.getByLabelText("Entry zone high"), "2432");
  await userEvent.type(screen.getByLabelText("Stop loss"), "2421");
};

const sentBody = () => JSON.parse(fetchMock.mock.calls[0][1].body as string);

describe("what it will not send", () => {
  it("will not review without both edges of the zone, and says so", async () => {
    open();

    const review = screen.getByRole("button", { name: /Review BUY/ });
    expect(review).toBeDisabled();
    expect(review).toHaveAttribute("title", expect.stringContaining("both edges"));
  });

  it("will not review a resting order with no stop loss", async () => {
    // It can fill at any time, including while nothing is watching.
    open();
    await userEvent.type(screen.getByLabelText("Entry zone low"), "2430");
    await userEvent.type(screen.getByLabelText("Entry zone high"), "2432");

    const review = screen.getByRole("button", { name: /Review BUY/ });
    expect(review).toBeDisabled();
    expect(review).toHaveAttribute("title", expect.stringContaining("stop loss"));
  });

  it("sends nothing on the first click", async () => {
    open();
    await fill();

    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));

    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("the confirmation", () => {
  it("names the instrument, direction, size and both edges of the zone", async () => {
    open();
    await fill();
    await userEvent.type(screen.getByLabelText("Lots"), "0.05");

    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));

    const summary = screen.getByTestId("limit-order-summary");
    expect(summary).toHaveTextContent("BUY XAUUSD");
    expect(summary).toHaveTextContent("0.05 lots");
    expect(summary).toHaveTextContent("2430.00 and 2432.00");
    expect(summary).toHaveTextContent("stop loss 2421.00");
  });

  it("says the order can fill while nothing is watching", async () => {
    open();
    await fill();

    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));

    expect(screen.getByText(/while nothing is watching/)).toBeInTheDocument();
  });

  it("offers a way back that is not the confirm button", async () => {
    open();
    await fill();
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));

    await userEvent.click(screen.getByRole("button", { name: /Back/ }));

    expect(screen.getByRole("button", { name: /Review BUY/ })).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("what gets sent", () => {
  it("posts the zone, the stop and the direction", async () => {
    open();
    await fill();
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
    await userEvent.click(screen.getByRole("button", { name: /Place this BUY/ }));

    expect(fetchMock.mock.calls[0][0]).toBe("/api/trading/orders/limit");
    expect(sentBody()).toMatchObject({
      direction: "BUY", entry_low: 2430, entry_high: 2432, stop_loss: 2421,
    });
  });

  it("leaves an unset target as null so the ladder just stops there", async () => {
    open();
    await fill();
    await userEvent.type(screen.getByLabelText("TP1"), "2440");
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
    await userEvent.click(screen.getByRole("button", { name: /Place this BUY/ }));

    expect(sentBody().tp1).toBe(2440);
    expect(sentBody().tp2).toBeNull();
    expect(sentBody().tp8).toBeNull();
  });

  it("leaves a blank lot size as null so risk settings decide it", async () => {
    open();
    await fill();
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
    await userEvent.click(screen.getByRole("button", { name: /Place this BUY/ }));

    expect(sentBody().lot_size).toBeNull();
  });
});

describe("when trading is not allowed", () => {
  it("disables the control and shows the reason", async () => {
    const why = "Out of hours — the schedule has no window open.";
    open({ disabledReason: why });

    const review = screen.getByRole("button", { name: /Review BUY/ });
    expect(review).toBeDisabled();
    expect(review).toHaveAttribute("title", why);
    expect(screen.getByRole("status")).toHaveTextContent(why);
  });
});

describe("when the backend refuses", () => {
  it("shows the backend's own words", async () => {
    fetchMock.mockResolvedValue({
      ok: false, status: 409, statusText: "",
      json: async () => ({
        error: {
          kind: "refusal",
          message: "The EA is not attached, so a resting order cannot be placed.",
          ref: null,
        },
      }),
    });
    open();
    await fill();
    await userEvent.click(screen.getByRole("button", { name: /Review BUY/ }));
    await userEvent.click(screen.getByRole("button", { name: /Place this BUY/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The EA is not attached",
    );
  });
});
