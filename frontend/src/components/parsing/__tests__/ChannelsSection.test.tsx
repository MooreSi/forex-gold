import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ChannelsSection } from "../internal/ChannelsSection";
import type { ParsingChannel } from "@/api/types";

/**
 * Turning a channel's parser on and off.
 *
 * Reported by the owner, 2026-09-21: "parsing > channels - cannot select,
 * deselect the telegram channels to parse". They could not. The endpoint
 * called the config writer with two arguments where it takes six, so every
 * click raised a TypeError and answered 500.
 *
 * The write is fixed in the backend and pinned in
 * tests/services/channels/test_parser_enable_toggle.py. What is pinned HERE is
 * the other half of why it was so hard to see: a refused write left no mark on
 * the screen at all, so a hard 500 looked like a box that would not stick.
 */
function channel(name: string, parser: Record<string, unknown> = {}): ParsingChannel {
  return { name, parser } as ParsingChannel;
}

describe("the checkboxes", () => {
  it("is ticked for a channel that is being parsed", () => {
    render(<ChannelsSection channels={[channel("GoldSignals", { enabled: true })]}
                            onToggle={async () => {}} />);

    expect(screen.getByLabelText("Parse GoldSignals")).toBeChecked();
  });

  it("is ticked for a channel with no parser row yet", () => {
    // The list is built from stored messages, so a channel can appear before
    // anything has configured it. Unticked would claim it is being ignored.
    render(<ChannelsSection channels={[channel("GoldSignals")]} onToggle={async () => {}} />);

    expect(screen.getByLabelText("Parse GoldSignals")).toBeChecked();
  });

  it("is unticked when the row says enabled=0", () => {
    // SQLite stores the flag as an integer, not a boolean. Testing only
    // `!== false` left every disabled channel looking enabled.
    render(<ChannelsSection channels={[channel("GoldSignals", { enabled: 0 })]}
                            onToggle={async () => {}} />);

    expect(screen.getByLabelText("Parse GoldSignals")).not.toBeChecked();
  });

  it("sends the new state when it is unticked", async () => {
    const toggle = vi.fn(async () => {});
    render(<ChannelsSection channels={[channel("GoldSignals", { enabled: true })]}
                            onToggle={toggle} />);

    await userEvent.click(screen.getByLabelText("Parse GoldSignals"));

    expect(toggle).toHaveBeenCalledWith("GoldSignals", false);
  });

  it("sends the new state when it is ticked again", async () => {
    const toggle = vi.fn(async () => {});
    render(<ChannelsSection channels={[channel("GoldSignals", { enabled: 0 })]}
                            onToggle={toggle} />);

    await userEvent.click(screen.getByLabelText("Parse GoldSignals"));

    expect(toggle).toHaveBeenCalledWith("GoldSignals", true);
  });
});

describe("when the write is refused", () => {
  it("says so instead of just springing back", async () => {
    // The reported bug was a 500 on every click, and this screen said nothing
    // about it. Silence is what turned a backend error into "the checkbox is
    // broken".
    render(
      <ChannelsSection
        channels={[channel("GoldSignals", { enabled: true })]}
        onToggle={async () => { throw new Error("Unknown channel 'GoldSignals'."); }}
      />,
    );

    await userEvent.click(screen.getByLabelText("Parse GoldSignals"));

    expect(await screen.findByRole("alert")).toHaveTextContent("Unknown channel");
  });

  it("clears the complaint when the next attempt works", async () => {
    let fail = true;
    render(
      <ChannelsSection
        channels={[channel("GoldSignals", { enabled: true })]}
        onToggle={async () => { if (fail) throw new Error("nope"); }}
      />,
    );
    await userEvent.click(screen.getByLabelText("Parse GoldSignals"));
    await screen.findByRole("alert");

    fail = false;
    await userEvent.click(screen.getByLabelText("Parse GoldSignals"));

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("when there are none", () => {
  it("says where to add them", () => {
    render(<ChannelsSection channels={[]} onToggle={async () => {}} />);

    expect(screen.getByText("No channels configured")).toBeInTheDocument();
  });
});
