import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { TemplateEditor } from "../internal/TemplateEditor";
import { FIELD_GROUPS } from "../content/templateGroups";
import { FIELD_HELP, helpFor } from "../content/templateHelp";

/**
 * The owner's tidy-up of the EA template form, 2026-10-01:
 *
 *  - "on take profit ladder both anchor and pending under each tp1, tp2 pips
 *    directly under have the box called Take % ... also make each box less
 *    wide so you fit 4 in a row"
 *  - "why it say Grid legs (legacy)? i thought this would be used if pending
 *    legs was selected?" -- it is not: Pending legs replaced it, and the EA
 *    only reads it when Pending legs is missing, which never happens for a
 *    template saved from here. So it is not offered.
 *  - "add tool tips when you hover over each box".
 */
const ladder = (prefix: string) => [1, 2, 3, 4, 5].flatMap((n) => [
  { name: `${prefix}${n}_pips`, type: "number", default: 0, choices: [] },
  { name: `${prefix}${n}_pct`, type: "number", default: 0, choices: [] },
]);

const SCHEMA = [
  { name: "mode", type: "choice", default: "single", choices: ["single", "grid"] },
  { name: "pendings", type: "integer", default: 1, choices: [] },
  { name: "grid_legs", type: "integer", default: 3, choices: [] },
  { name: "sl_pips", type: "number", default: 50, choices: [] },
  { name: "signal_rr_ratio", type: "number", default: 0, choices: [] },
  ...ladder("tp"),
  { name: "tp_pen_from_telegram", type: "boolean", default: false, choices: [] },
  ...ladder("tp_pen"),
];

function renderEditor() {
  const onSave = vi.fn(
    async (_name: string, _values: Record<string, unknown>) => ({ pushed: true }),
  );
  render(
    <TemplateEditor
      name="GD VIP - Single"
      values={{ name: "GD VIP - Single", grid_legs: 5, tp1_pips: 30, tp1_pct: 50 }}
      schema={SCHEMA}
      onSave={onSave}
      onClose={() => {}}
    />,
  );
  return { onSave };
}

describe("the TP ladders", () => {
  it.each([
    ["tp", "TP"],
    ["tp_pen", "Pending TP"],
  ])("%s ladder: each level's Take box sits directly under its pips", (prefix, label) => {
    renderEditor();

    for (const n of [1, 2, 3, 4, 5]) {
      const card = screen.getByTestId(`ladder-${prefix}-${n}`);
      const boxes = within(card).getAllByRole("textbox");
      expect(boxes.map((b) => b.getAttribute("aria-label"))).toEqual([
        `${label}${n} pips`, `${label}${n} Take %`,
      ]);
    }
  });

  it("lays the levels out four to a row", () => {
    renderEditor();

    const grid = screen.getByTestId("ladder-tp-1").parentElement!;
    expect(grid.className).toMatch(/\bsm:grid-cols-4\b/);
  });

  it("keeps the signal's-targets switch outside the level grid", () => {
    renderEditor();

    const grid = screen.getByTestId("ladder-tp_pen-1").parentElement!;
    expect(within(grid).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.getByLabelText(/pendings use the signal's targets/i)).toBeInTheDocument();
  });

  it("still sends every ladder value on save", async () => {
    const { onSave } = renderEditor();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    const sent = onSave.mock.calls[0][1];
    expect(sent["tp1_pips"]).toBe(30);
    expect(sent["tp1_pct"]).toBe(50);
    expect(sent).toHaveProperty("tp_pen5_pct");
  });
});

describe("grid legs", () => {
  it("is not offered, because Pending legs replaced it", () => {
    renderEditor();

    expect(screen.queryByLabelText(/grid legs/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Pending legs")).toBeInTheDocument();
  });

  it("keeps its stored value rather than resetting it", async () => {
    // Hidden is not deleted: a save sends every field, and an omitted one
    // would reach `_clean_fields` as "use the default".
    const { onSave } = renderEditor();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(onSave.mock.calls[0][1]["grid_legs"]).toBe(5);
  });
});

describe("hover help", () => {
  it("explains a setting when you hover over its box", async () => {
    renderEditor();
    await userEvent.hover(screen.getByLabelText("Stop distance"));

    expect(await screen.findByRole("tooltip")).toHaveTextContent(FIELD_HELP["sl_pips"]);
  });

  it("explains a ladder box too", async () => {
    renderEditor();
    await userEvent.hover(screen.getByLabelText("TP2 Take %"));

    expect(await screen.findByRole("tooltip")).toHaveTextContent(/share of the original position/);
  });

  it("has written help for every field the form groups by name", () => {
    const named = FIELD_GROUPS.flatMap((g) => g.fields ?? []);
    const missing = named.filter((n) => !FIELD_HELP[n]);
    expect(missing).toEqual([]);
  });

  it("falls back to naming a field nobody has written help for", () => {
    // Negative control for the test above: the fallback is not mistaken
    // for written help.
    expect(FIELD_HELP["some_new_field"]).toBeUndefined();
    expect(helpFor("some_new_field")).toContain("some_new_field");
  });

  it("says plainly when a setting is not acted on", () => {
    expect(helpFor("auto_sl")).toMatch(/no effect/);
    expect(helpFor("sl_pips")).not.toMatch(/no effect/);
  });
});

it("describes the R:R setting as the filter it is", () => {
  // signals/resolution.py SKIPS a signal whose own TP1:SL is below this. The
  // old label said it set a target, which is the opposite of what it does.
  renderEditor();

  expect(screen.queryByLabelText(/target r:r/i)).not.toBeInTheDocument();
  expect(screen.getByLabelText(/skip a signal/i)).toBeInTheDocument();
});
