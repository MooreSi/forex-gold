/**
 * The brain view renders the backend's answer as given: which gates are
 * holding orders now, and one plain line per recorded decision. It has no
 * control of any kind (docs/todo/008).
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BrainView } from "../BrainView";
import type { BrainEvent, BrainGate } from "../content/brainLayout";

const GATES: BrainGate[] = [
  { key: "halt", label: "Pause & halts", blocking: true, detail: "Daily loss limit hit" },
  { key: "breaker", label: "Circuit breaker", blocking: null, detail: "" },
  { key: "slots", label: "Open-trade slots", blocking: false, detail: "1 of 3 in use" },
  { key: "broker", label: "Broker", blocking: null, detail: "" },
];
const EVENTS: BrainEvent[] = [
  { key: "tg:1", ts: 100, kind: "telegram", source: "GOLD X", direction: "BUY",
    outcome: "executed", gate: "broker", reason: "" },
  { key: "tg:2", ts: 200, kind: "telegram", source: "GOLD Y", direction: "SELL",
    outcome: "blocked", gate: "slots", reason: "max open trades (3) reached" },
];

describe("BrainView", () => {
  it("names the gates holding orders now, with why", () => {
    render(<BrainView gates={GATES} events={EVENTS} />);
    expect(screen.getByTestId("brain-holding"))
      .toHaveTextContent("Holding orders now: Pause & halts (Daily loss limit hit)");
  });

  it("an unreadable gate is drawn as unknown, not as open", () => {
    render(<BrainView gates={GATES} events={EVENTS} />);
    expect(screen.getByTestId("brain-gate-breaker")).toHaveAttribute("data-blocking", "null");
    expect(screen.getByTestId("brain-gate-halt")).toHaveAttribute("data-blocking", "true");
  });

  it("says so when nothing is holding orders", () => {
    render(<BrainView gates={GATES.map((g) => ({ ...g, blocking: false }))} events={EVENTS} />);
    expect(screen.getByTestId("brain-holding")).toHaveTextContent("No gate is holding orders right now.");
  });

  it("writes one line per decision, newest first", () => {
    render(<BrainView gates={GATES} events={EVENTS} />);
    const lines = screen.getAllByTestId(/brain-thought-/);
    expect(lines[0]).toHaveTextContent("GOLD Y SELL: held at Open-trade slots: max open trades (3) reached");
    expect(lines[1]).toHaveTextContent("GOLD X BUY: passed every gate, sent to the broker");
  });

  it("has no controls", () => {
    render(<BrainView gates={GATES} events={EVENTS} />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("before the first read it says it is waking up", () => {
    render(<BrainView gates={undefined} events={undefined} />);
    expect(screen.getByText("Waking up")).toBeInTheDocument();
  });
});
