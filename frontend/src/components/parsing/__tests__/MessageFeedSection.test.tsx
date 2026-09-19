import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { MessageFeedSection } from "../internal/MessageFeedSection";

/**
 * The stored Telegram feed, one tab per channel.
 *
 * Two things the owner asked for on 2026-09-19: a table per channel rather
 * than one mixed stream, and a feed that stops looking like it is updating
 * when nothing has arrived. The second was a backend fix (`id` was missing
 * from the SELECT, so React keyed the list on array position) — the part that
 * belongs here is that rows are keyed on that identity and never on position.
 */
function msg(id: number, over: Record<string, unknown> = {}) {
  return {
    id,
    group_name: "GoldSignals",
    text: `message ${id}`,
    timestamp: 1757955600 + id,
    ...over,
  };
}

describe("grouping by channel", () => {
  it("offers a tab per channel, plus everything", async () => {
    render(<MessageFeedSection total={3} messages={[
      msg(1), msg(2, { group_name: "NoisyChannel" }), msg(3),
    ]} />);

    expect(screen.getByRole("tab", { name: /All channels/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /GoldSignals/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /NoisyChannel/ })).toBeInTheDocument();
  });

  it("counts the messages in each channel's tab", async () => {
    // Which channel is noisy is the question this view is for.
    render(<MessageFeedSection total={3} messages={[
      msg(1), msg(2, { group_name: "NoisyChannel" }), msg(3),
    ]} />);

    expect(screen.getByRole("tab", { name: /GoldSignals/ })).toHaveTextContent("2");
    expect(screen.getByRole("tab", { name: /NoisyChannel/ })).toHaveTextContent("1");
  });

  it("shows only that channel's messages once one is chosen", async () => {
    render(<MessageFeedSection total={3} messages={[
      msg(1), msg(2, { group_name: "NoisyChannel" }), msg(3),
    ]} />);

    await userEvent.click(screen.getByRole("tab", { name: /NoisyChannel/ }));
    const table = screen.getByTestId("feed-table");

    expect(within(table).getByText("message 2")).toBeInTheDocument();
    expect(within(table).queryByText("message 1")).not.toBeInTheDocument();
  });

  it("opens on everything, so nothing is hidden by default", async () => {
    render(<MessageFeedSection total={2} messages={[
      msg(1), msg(2, { group_name: "NoisyChannel" }),
    ]} />);
    const table = screen.getByTestId("feed-table");

    expect(within(table).getByText("message 1")).toBeInTheDocument();
    expect(within(table).getByText("message 2")).toBeInTheDocument();
  });

  it("puts a message with no channel under one honest name", async () => {
    // Not blank, and not silently merged into whichever channel came first.
    render(<MessageFeedSection total={1} messages={[msg(1, { group_name: null })]} />);

    expect(screen.getByRole("tab", { name: /unknown/i })).toBeInTheDocument();
  });
});

describe("the rows", () => {
  it("keys each row on the message id, not its position", async () => {
    // The bug behind "the feed updates when nothing arrives": with positional
    // keys one arrival rewrites every row.
    //
    // A React key leaves no trace in the DOM, so asserting on a testid would
    // pass either way -- it did, when this was first written. What IS
    // observable is node identity across a re-render: keyed by id, the row
    // for message 41 is the SAME DOM node after a new message arrives; keyed
    // by position, React reuses node 0 for the newcomer and 41's content is
    // rewritten into a different element.
    const { rerender } = render(
      <MessageFeedSection total={2} messages={[msg(42), msg(41)]} />,
    );
    const before = screen.getByTestId("feed-row-41");

    rerender(
      <MessageFeedSection total={3} messages={[msg(43), msg(42), msg(41)]} />,
    );

    expect(screen.getByTestId("feed-row-41")).toBe(before);
  });

  it("renders the ISO timestamp the column actually holds", async () => {
    // `telegram_messages.timestamp` is TEXT, not an epoch. The first version
    // of this accepted only a number, so every row in the feed showed an em
    // dash where its time should be. Checked against the live payload.
    render(<MessageFeedSection total={1} messages={[
      msg(1, { timestamp: "2026-09-18T16:26:44+00:00" }),
    ]} />);

    expect(screen.getByTestId("feed-row-1")).toHaveTextContent("18 Sept, 17:26");
  });

  it("does not apply the broker offset to a Telegram stamp", async () => {
    // These are real UTC from Telegram, not MT5 broker time. Subtracting the
    // three-hour broker offset would put every message three hours early --
    // and 14:26 is exactly what that mistake looks like.
    render(<MessageFeedSection total={1} messages={[
      msg(1, { timestamp: "2026-09-18T16:26:44+00:00" }),
    ]} />);

    expect(screen.getByTestId("feed-row-1")).not.toHaveTextContent("14:26");
  });

  it("falls back to received_at when the sent time is missing", async () => {
    render(<MessageFeedSection total={1} messages={[
      msg(1, { timestamp: null, received_at: "2026-09-18T16:26:44+00:00" }),
    ]} />);

    expect(screen.getByTestId("feed-row-1")).toHaveTextContent("17:26");
  });

  it("shows a dash for a stamp it cannot read", async () => {
    render(<MessageFeedSection total={1} messages={[msg(1, { timestamp: "not a date" })]} />);

    expect(screen.getByTestId("feed-row-1")).toHaveTextContent("—");
  });

  it("shows the message text", async () => {
    render(<MessageFeedSection total={1} messages={[msg(1, { text: "BUY XAUUSD 4000" })]} />);

    expect(screen.getByText("BUY XAUUSD 4000")).toBeInTheDocument();
  });

  it("says a message had media rather than showing an empty row", async () => {
    render(<MessageFeedSection total={1} messages={[
      msg(1, { text: "", has_media: 1, media_type: "photo" }),
    ]} />);

    expect(screen.getByTestId("feed-row-1")).toHaveTextContent(/photo/i);
  });

  it("survives a row that is missing everything", async () => {
    // The feed is read straight from a table the reader writes; a row with a
    // null text must not blank the panel.
    render(<MessageFeedSection total={1} messages={[{ id: 9 }]} />);

    expect(screen.getByTestId("feed-row-9")).toBeInTheDocument();
  });
});

describe("when there is nothing", () => {
  it("says so", async () => {
    render(<MessageFeedSection total={0} messages={[]} />);

    expect(screen.getByText("No stored messages")).toBeInTheDocument();
  });

  it("says how many of the total are on screen", async () => {
    render(<MessageFeedSection total={840} messages={[msg(1), msg(2)]} />);

    expect(screen.getByTestId("feed-count")).toHaveTextContent("840");
  });
});
