/**
 * The signal feed says what HAPPENED, not how a row stopped.
 *
 * "Closed" was the same word for a signal that took 300 dollars and one that
 * gave back 300 — 532 of the owner's 608 signals carried it — so the feed was
 * a list of finished things with no way to read the record off it. Owner,
 * 2026-09-22: won, lost, open or skipped, colour coded.
 *
 * The words are chosen here; the FACT is not. `outcome` is decided by
 * services/signals/outcomes.py from the trades a signal produced, and this
 * card renders it. Summing P&L in the browser would be a second answer to
 * "did this win".
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SignalFeedCard } from "../internal/SignalFeedCard";

const base = { id: "1", source: "ICT Signals", direction: "BUY", entry: 2412.6 };

const pill = (label: string) => screen.getByText(label);

describe("what each signal is labelled", () => {
  it("calls a closed signal that made money WON", () => {
    render(<SignalFeedCard signals={[{ ...base, status: "closed", outcome: "won", net_pnl: 25.3 }]} />);

    expect(pill("won")).toBeInTheDocument();
    expect(screen.queryByText("closed")).toBeNull();
  });

  it("calls a closed signal that lost money LOST", () => {
    render(<SignalFeedCard signals={[{ ...base, status: "closed", outcome: "lost", net_pnl: -59.4 }]} />);

    expect(pill("lost")).toBeInTheDocument();
  });

  it("calls a break-even signal FLAT, not won", () => {
    // The backend's third answer. Folding it into "won" here would put the
    // scratch trades back into the win column at the last moment.
    render(<SignalFeedCard signals={[{ ...base, status: "closed", outcome: "flat", net_pnl: 0 }]} />);

    expect(pill("flat")).toBeInTheDocument();
  });

  it("calls a live signal OPEN", () => {
    render(<SignalFeedCard signals={[{ ...base, status: "active", outcome: null, net_pnl: null }]} />);

    expect(pill("open")).toBeInTheDocument();
  });

  it("calls an expired or cancelled signal SKIPPED", () => {
    // Neither traded. "Skipped" is what the operator calls that, and it is
    // not a loss — a feed that showed it in red would read as a losing run.
    render(<SignalFeedCard signals={[
      { ...base, id: "e", status: "expired", outcome: null, net_pnl: null },
      { ...base, id: "c", status: "cancelled", outcome: null, net_pnl: null },
    ]} />);

    expect(screen.getAllByText("skipped")).toHaveLength(2);
  });

  it("prefers the outcome over the status when a cancelled signal still traded", () => {
    // Three signals in the owner's own database are cancelled AND have a
    // winning trade against them: the signal was withdrawn after the
    // position was taken. The money happened, so the money is the answer;
    // labelling it "skipped" would hide a real win.
    render(<SignalFeedCard signals={[
      { ...base, status: "cancelled", outcome: "won", net_pnl: 31.2 },
    ]} />);

    expect(pill("won")).toBeInTheDocument();
    expect(screen.queryByText("skipped")).toBeNull();
  });

  it("shows an unknown status as itself rather than forcing it into one of the four", () => {
    // `pending` is reachable (create_signal writes it) and is neither open
    // nor skipped — it may still be taken. Inventing a label for a state
    // nobody has described is how a screen starts lying quietly.
    render(<SignalFeedCard signals={[{ ...base, status: "pending", outcome: null, net_pnl: null }]} />);

    expect(pill("pending")).toBeInTheDocument();
  });
});

describe("the colours", () => {
  const toneOf = (label: string) => pill(label).className;

  it("greens a win and reds a loss, as everywhere else in the app", () => {
    render(<SignalFeedCard signals={[
      { ...base, id: "w", status: "closed", outcome: "won", net_pnl: 10 },
      { ...base, id: "l", status: "closed", outcome: "lost", net_pnl: -10 },
    ]} />);

    expect(toneOf("won")).toMatch(/text-profit/);
    expect(toneOf("lost")).toMatch(/text-loss/);
  });

  it("does not colour a skipped signal as a loss", () => {
    render(<SignalFeedCard signals={[{ ...base, status: "expired", outcome: null, net_pnl: null }]} />);

    expect(toneOf("skipped")).not.toMatch(/text-loss/);
    expect(toneOf("skipped")).toMatch(/text-ink-3/);
  });

  it("marks an open signal with the app's accent, not with profit green", () => {
    // It has not won anything yet. Green would be a claim about money.
    render(<SignalFeedCard signals={[{ ...base, status: "active", outcome: null, net_pnl: null }]} />);

    expect(toneOf("open")).toMatch(/text-accent/);
    expect(toneOf("open")).not.toMatch(/text-profit/);
  });
});

describe("the money behind the label", () => {
  it("puts the realised P&L on the row's tooltip", () => {
    render(<SignalFeedCard signals={[{ ...base, status: "closed", outcome: "won", net_pnl: 25.3 }]} />);

    expect(pill("won").getAttribute("title")).toContain("+$25.30");
  });
});

describe("a feed longer than the card", () => {
  // Owner, 2026-09-26: the card stopped at six rows and there was no way to
  // read further down it. Every signal it is handed is now a row, and the
  // list scrolls inside the card rather than stretching the dashboard grid.
  const many = Array.from({ length: 20 }, (_, i) => ({
    ...base, id: String(i), source: `Source ${i}`, status: "closed", outcome: "won", net_pnl: 1,
  }));

  it("renders every signal, not only the first six", () => {
    render(<SignalFeedCard signals={many} />);

    expect(screen.getAllByRole("listitem")).toHaveLength(20);
    expect(screen.getByText("Source 19")).toBeInTheDocument();
  });

  it("scrolls the list inside a bounded height", () => {
    render(<SignalFeedCard signals={many} />);

    const list = screen.getByRole("list");
    expect(list.className).toMatch(/overflow-y-auto/);
    expect(list.className).toMatch(/max-h-/);
  });
});
