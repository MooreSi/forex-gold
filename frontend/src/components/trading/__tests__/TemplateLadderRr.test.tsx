/**
 * The R:R readout inside the EA template editor.
 *
 * A template is a strategy, and the question the operator is actually asking
 * while editing one is "what does this pay against what it risks". That is
 * pips over the stop — a field in a different group, three sections up — so
 * without a readout it can only be done by eye, or not at all.
 *
 * It has to move as the form does. These tests are about the wiring: that the
 * readout sits under the ladder it describes, that it follows the stop, the
 * percentages and the close-full switch, and that the two ladders are read
 * separately. The arithmetic itself is pinned by ladderRr.test.ts against the
 * same case file the backend is pinned against.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { TemplateEditor } from "../internal/TemplateEditor";

const SCHEMA = [
  { name: "sl_pips", type: "number", default: 50, choices: [] },
  { name: "close_full_on_last", type: "boolean", default: false, choices: [] },
  { name: "tp_from_telegram", type: "boolean", default: false, choices: [] },
  { name: "tp1_pips", type: "number", default: 40, choices: [] },
  { name: "tp1_pct", type: "number", default: 50, choices: [] },
  { name: "tp2_pips", type: "number", default: 80, choices: [] },
  { name: "tp2_pct", type: "number", default: 50, choices: [] },
  { name: "tp_pen1_pips", type: "number", default: 100, choices: [] },
  { name: "tp_pen1_pct", type: "number", default: 100, choices: [] },
];

const VALUES = {
  name: "Asian - Single", sl_pips: 40, close_full_on_last: false,
  tp_from_telegram: false, tp1_pips: 40, tp1_pct: 50, tp2_pips: 80,
  tp2_pct: 50, tp_pen1_pips: 100, tp_pen1_pct: 100,
};

function renderEditor(over: Record<string, unknown> = {}) {
  render(
    <TemplateEditor
      name="Asian - Single"
      values={{ ...VALUES, ...over }}
      schema={SCHEMA}
      onSave={vi.fn(async () => ({ pushed: false }))}
      onClose={() => {}}
    />,
  );
}

describe("each ladder gets its own readout", () => {
  it("values the anchor ladder against the stop", () => {
    // 40 and 80 pips over a 40-pip stop, half closing at each: 0.5R + 1.0R.
    renderEditor();

    expect(screen.getByTestId("rr-summary-tp")).toHaveTextContent("1.50R");
  });

  it("values the pending ladder separately", () => {
    // 100 pips over the same stop, all of it closing there.
    renderEditor();

    expect(screen.getByTestId("rr-summary-tp_pen")).toHaveTextContent("2.50R");
  });
});

describe("the readout follows the form", () => {
  it("re-values every ladder when the stop changes", async () => {
    // The stop lives in its own group further up, which is the whole reason
    // this readout has to exist rather than be worked out by eye.
    const user = userEvent.setup();
    renderEditor();

    await user.clear(screen.getByLabelText(/stop distance/i));
    await user.type(screen.getByLabelText(/stop distance/i), "20");

    expect(screen.getByTestId("rr-summary-tp")).toHaveTextContent("3.00R");
    expect(screen.getByTestId("rr-summary-tp_pen")).toHaveTextContent("5.00R");
  });

  it("re-values when a level's share of the position changes", async () => {
    const user = userEvent.setup();
    renderEditor();

    await user.clear(screen.getByLabelText(/^tp1 pct$/i));
    await user.type(screen.getByLabelText(/^tp1 pct$/i), "20");

    // 1.0R x 20% + 2.0R x 50% = 1.20R, and 30% is left running.
    expect(screen.getByTestId("rr-summary-tp")).toHaveTextContent("1.20R");
    expect(screen.getByTestId("rr-summary-tp")).toHaveTextContent(/30% left running/i);
  });

  it("re-values when close-full-on-last is switched on", async () => {
    const user = userEvent.setup();
    renderEditor();

    await user.click(screen.getByLabelText(/close everything at the last target/i));

    // TP2 now banks the 50% TP1 left rather than its own 50% — same number
    // here, but the runner is gone, and that is what the line has to say.
    expect(screen.getByTestId("rr-summary-tp")).toHaveTextContent("(closes 100%)");
  });
});
