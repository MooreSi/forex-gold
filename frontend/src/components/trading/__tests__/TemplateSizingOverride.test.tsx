import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { TemplateEditor } from "../internal/TemplateEditor";

/**
 * With Trading > Risk's EA template override on, a template's own lot sizes
 * and risk % size nothing, so the editor must not offer them as if they did.
 * docs/todo/risk/010.
 */
const SCHEMA = [
  { name: "lot_anchor", type: "number", default: 0.01, choices: [] },
  { name: "lot_pending", type: "number", default: 0.01, choices: [] },
  { name: "risk_pct", type: "number", default: 0, choices: [] },
  { name: "sl_pips", type: "number", default: 50, choices: [] },
];
const VALUES = { name: "T", lot_anchor: 0.1, lot_pending: 0.05, risk_pct: 0, sl_pips: 50 };

function renderEditor(sizingOverridden: boolean) {
  const onSave = vi.fn(
    async (_name: string, _values: Record<string, unknown>) => ({ pushed: false }),
  );
  render(
    <TemplateEditor name="T" values={VALUES} schema={SCHEMA} onSave={onSave}
      onClose={() => {}} sizingOverridden={sizingOverridden} />,
  );
  return { onSave };
}

describe("the template's own sizing fields", () => {
  it("are greyed out and say why while the override is on", () => {
    renderEditor(true);

    for (const label of ["Lots per anchor leg", "Lots per pending leg", "Risk per trade"]) {
      expect(screen.getByLabelText(label)).toBeDisabled();
    }
    expect(screen.getAllByText(/Not in use: the EA template override/)).toHaveLength(3);
  });

  it("keep their values, so switching the override off restores them", async () => {
    const { onSave } = renderEditor(true);

    expect(screen.getByLabelText("Lots per anchor leg")).toHaveValue("0.1");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave.mock.calls[0][1]).toMatchObject({ lot_anchor: 0.1, lot_pending: 0.05 });
  });

  it("leave every other field alone", () => {
    renderEditor(true);

    expect(screen.getByLabelText("Stop distance")).toBeEnabled();
  });

  it("are editable while the override is off", () => {
    renderEditor(false);

    expect(screen.getByLabelText("Lots per anchor leg")).toBeEnabled();
    expect(screen.queryByText(/Not in use: the EA template override/)).not.toBeInTheDocument();
  });
});
