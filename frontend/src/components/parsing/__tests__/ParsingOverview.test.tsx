import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ChannelsSection } from "../internal/ChannelsSection";
import { ParsingToggleCard } from "../internal/ParsingToggleCard";
import { ReaderStatusSection } from "../internal/ReaderStatusSection";
import { SignalsSourcesSection } from "../internal/SignalsSourcesSection";

/**
 * The 2026-09-30 redesign of the Telegram (Parsing) tab: what it added, not
 * how it looks. ParsingPanel.test.tsx still pins everything that was there.
 */

const READER = {
  auth_state: "connected",
  messages_this_session: 64,
  messages_stored_total: 4634,
  last_message_at: new Date(Date.now() - 120_000).toISOString(),
  recent_errors: [],
  slots: [
    { slot: 1, group_name: "Gold Diggers VIP", listener_active: true, poller_active: true },
    { slot: 2, group_name: "GOLD DIGGERS INSTITUTIONAL", listener_active: false, poller_active: false,
      last_poll_error: "FloodWaitError" },
  ],
};

describe("the status strip", () => {
  const strip = (over: Partial<Parameters<typeof ReaderStatusSection>[0]> = {}) =>
    render(
      <ReaderStatusSection
        reader={READER}
        configured
        settings={{ auto_execute_signals: 1 }}
        controlTarget="local"
        {...over}
      />,
    );

  it("says how many channels are actually listening", () => {
    strip();
    expect(screen.getByTestId("reader-status")).toHaveTextContent("Connected");
    expect(screen.getByTestId("reader-status")).toHaveTextContent("1/2 channels listening");
  });

  it("says the VPS trades Telegram when it is the active trader", () => {
    // Since v0.612 the node that trades reads and parses Telegram itself.
    strip({ controlTarget: "remote" });
    expect(screen.getByText("The VPS")).toBeInTheDocument();
  });

  it("counts centralized mode as the VPS trading too", () => {
    strip({ controlTarget: "centralized" });
    expect(screen.getByText("The VPS")).toBeInTheDocument();
  });

  it("says this machine when it is the trader", () => {
    strip();
    expect(screen.getByText("This machine")).toBeInTheDocument();
    expect(screen.queryByText("The VPS")).not.toBeInTheDocument();
  });

  it("does not claim auto-execution while Telegram signals are off", () => {
    strip({ settings: { auto_execute_signals: 1, accept_tg_signals: 0 } });
    expect(screen.getByText("Telegram signals off")).toBeInTheDocument();
  });

  it("surfaces the reader's own errors", () => {
    strip({ reader: { ...READER, recent_errors: ["old", "AuthKeyUnregistered"] } });
    expect(screen.getByRole("alert")).toHaveTextContent("AuthKeyUnregistered");
  });

  it("says not set up, not connected, on an install with no reader", () => {
    strip({ reader: {}, configured: false });
    expect(screen.getByTestId("reader-status")).toHaveTextContent("Not set up");
  });
});

describe("the Trend PA source", () => {
  const sources = (settings: Record<string, unknown> = {}) => {
    const onSave = vi.fn();
    render(<SignalsSourcesSection settings={settings} onSave={onSave} />);
    return { onSave, card: screen.getByTestId("source-tpa_live_execution") };
  };

  it("is on the list, off by default", () => {
    const { card } = sources();
    expect(within(card).getByRole("button", { name: "TPA LIVE OFF" })).toBeInTheDocument();
  });

  it("asks before it places real orders, and a Cancel sends nothing", async () => {
    const { card, onSave } = sources();
    await userEvent.click(within(card).getByRole("button", { name: "TPA LIVE OFF" }));

    expect(within(card).getByRole("alertdialog")).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Cancel" }));
    expect(onSave).not.toHaveBeenCalled();
  });

  it("turns on once confirmed", async () => {
    const { card, onSave } = sources();
    await userEvent.click(within(card).getByRole("button", { name: "TPA LIVE OFF" }));
    await userEvent.click(within(card).getByRole("button", { name: "Yes, place real orders" }));

    expect(onSave).toHaveBeenCalledWith("tpa_live_execution", 1);
  });

  it("turns off with one press", async () => {
    // Stopping an engine must never wait on a confirmation.
    const { card, onSave } = sources({ tpa_live_execution: 1 });
    await userEvent.click(within(card).getByRole("button", { name: "TPA LIVE ON" }));

    expect(onSave).toHaveBeenCalledWith("tpa_live_execution", 0);
  });
});

describe("channel listeners", () => {
  const CHANNELS = [
    { name: "Gold Diggers VIP", parser: { enabled: 1 } },
    { name: "GOLD DIGGERS INSTITUTIONAL", parser: { enabled: 1 } },
    { name: "Gold Diggers Scalping", parser: { enabled: 1 } },
  ];

  it("tells a listening channel from one whose listener is down", () => {
    render(<ChannelsSection channels={CHANNELS} onToggle={vi.fn()} slots={READER.slots} />);

    expect(screen.getByTestId("channel-Gold Diggers VIP")).toHaveTextContent("Listening");
    const down = screen.getByTestId("channel-GOLD DIGGERS INSTITUTIONAL");
    expect(down).toHaveTextContent("Listener down");
    expect(down).toHaveTextContent("FloodWaitError");
  });

  it("names a channel no reader slot is watching", () => {
    render(<ChannelsSection channels={CHANNELS} onToggle={vi.fn()} slots={READER.slots} />);
    expect(screen.getByTestId("channel-Gold Diggers Scalping"))
      .toHaveTextContent("Not assigned to a reader slot");
  });
});

describe("a long description", () => {
  const LONG = { key: "k", label: "Some switch", description: "x".repeat(200), defaultOn: false };

  it("folds to two lines and opens in place", async () => {
    render(<ParsingToggleCard toggle={LONG} on={false} onChange={vi.fn()} />);
    const text = screen.getByText(LONG.description);
    expect(text.className).toContain("line-clamp-2");

    await userEvent.click(screen.getByRole("button", { name: "More" }));
    expect(text.className).not.toContain("line-clamp-2");
  });
});
