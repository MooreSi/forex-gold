/**
 * The per-channel daily loss cap card on the Schedule screen.
 *
 * Held/not-held and today's P&L are the backend's answer, rendered as given.
 * What the card decides is only what it SENDS: the default cap plus the whole
 * override map, with a blank channel box meaning "use the default" (the
 * override is dropped), not "cap of 0".
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ChannelLossCapCard, type ChannelLossCapState } from "../internal/ChannelLossCapCard";

function state(over: Partial<ChannelLossCapState> = {}): ChannelLossCapState {
  return {
    default_cap: 100,
    overrides: { "GOLD Y": 40 },
    channels: [
      { channel: "GOLD X", cap: 100, day_pnl: -120, held: true },
      { channel: "GOLD Y", cap: 40, day_pnl: -10, held: false },
    ],
    error: "",
    ...over,
  };
}

describe("ChannelLossCapCard", () => {
  it("marks a held channel and leaves the others alone", () => {
    render(<ChannelLossCapCard cap={state()} onSave={vi.fn()} />);
    expect(screen.getByTestId("loss-cap-GOLD X")).toHaveTextContent(/Held/);
    expect(screen.getByTestId("loss-cap-GOLD Y")).not.toHaveTextContent(/Held/);
  });

  it("sends the new default with the overrides unchanged", async () => {
    const onSave = vi.fn();
    render(<ChannelLossCapCard cap={state()} onSave={onSave} />);
    const box = screen.getByLabelText("Cap for every channel");
    await userEvent.clear(box);
    await userEvent.type(box, "150");
    await userEvent.tab();
    expect(onSave).toHaveBeenLastCalledWith(150, { "GOLD Y": 40 });
  });

  it("sets one channel's own cap", async () => {
    const onSave = vi.fn();
    render(<ChannelLossCapCard cap={state()} onSave={onSave} />);
    await userEvent.type(screen.getByLabelText("Cap for GOLD X"), "60");
    await userEvent.tab();
    expect(onSave).toHaveBeenLastCalledWith(100, { "GOLD Y": 40, "GOLD X": 60 });
  });

  it("a cleared channel box drops the override instead of sending 0", async () => {
    const onSave = vi.fn();
    render(<ChannelLossCapCard cap={state()} onSave={onSave} />);
    await userEvent.clear(screen.getByLabelText("Cap for GOLD Y"));
    await userEvent.tab();
    expect(onSave).toHaveBeenLastCalledWith(100, {});
  });

  it("an unreadable state says so rather than showing no caps", () => {
    render(<ChannelLossCapCard cap={null} onSave={vi.fn()} />);
    expect(screen.getByText(/could not be read/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Cap for every channel")).toBeNull();
  });
});
