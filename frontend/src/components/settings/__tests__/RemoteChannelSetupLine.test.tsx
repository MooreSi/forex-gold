import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RemoteChannelSetupLine } from "../internal/RemoteChannelSetupLine";

/**
 * What the VPS did with this Mac's Telegram channel set-up (docs/todo/010).
 *
 * The VPS reads Telegram itself now. A channel it could not start listening on
 * is a channel whose signals will not trade, and nothing else on this screen
 * would say so: the Mac is still reading that channel for display.
 */
describe("RemoteChannelSetupLine", () => {
  it("says nothing before the VPS has answered", () => {
    const { container } = render(<RemoteChannelSetupLine result={{}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("says the set-up is in step when there is nothing wrong", () => {
    render(<RemoteChannelSetupLine result={{ errors: [], slots: [] }} />);
    expect(screen.getByTestId("remote-channel-setup")).toHaveTextContent(
      "Telegram channel set-up: in step with this machine");
  });

  it("names every problem the VPS reported", () => {
    render(<RemoteChannelSetupLine result={{
      errors: ["slot 3 (Gold Diggers Scalping): Not connected", "lexicon x"],
    }} />);
    const line = screen.getByTestId("remote-channel-setup");
    expect(line).toHaveTextContent("slot 3 (Gold Diggers Scalping): Not connected");
    expect(line).toHaveTextContent("lexicon x");
    expect(line.className).toContain("text-loss");
  });
});
