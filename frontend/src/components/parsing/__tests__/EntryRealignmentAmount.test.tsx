import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ParsingSettingsSection } from "../internal/ParsingSettingsSection";

/**
 * Entry Realignment's "realign up to N pips" (owner, 2026-10-02). The box
 * used to say "Limit Runner only", which had stopped being true and hid the
 * fact that EA templates never realigned a signal price had run away from.
 */
function renderBox(settings: Record<string, unknown> = {}) {
  const onSave = vi.fn(async () => {});
  render(<ParsingSettingsSection settings={settings} onSave={onSave} />);
  const box = screen.getByTestId("toggle-lk_entry_realignment");
  return { onSave, box, field: within(box).getByLabelText("Realign up to") };
}

describe("Entry Realignment pip limit", () => {
  it("sits inside the Entry Realignment box", () => {
    const { field } = renderBox();
    expect(field).toBeInTheDocument();
  });

  it("no longer claims to be Limit Runner only", () => {
    const { box } = renderBox();
    expect(box.textContent).not.toMatch(/Limit Runner only/);
    expect(box.textContent).toMatch(/EA templates/);
  });

  it("shows a stored limit", () => {
    const { field } = renderBox({ lk_entry_realignment_max_pips: 38 });
    expect(field).toHaveValue("38");
  });

  it("shows no limit as blank, not as 0", () => {
    const { field } = renderBox({ lk_entry_realignment_max_pips: 0 });
    expect(field).toHaveValue("");
  });

  it("saves the limit when the field is left", async () => {
    const { field, onSave } = renderBox();
    await userEvent.type(field, "40");
    await userEvent.tab();
    await waitFor(() => expect(onSave).toHaveBeenCalledWith("lk_entry_realignment_max_pips", 40));
  });

  it("saves a cleared field as 0, which means no limit", async () => {
    const { field, onSave } = renderBox({ lk_entry_realignment_max_pips: 38 });
    await userEvent.clear(field);
    await userEvent.tab();
    await waitFor(() => expect(onSave).toHaveBeenCalledWith("lk_entry_realignment_max_pips", 0));
  });

  it("does not flip the switch when the field or its text is clicked", async () => {
    const { box, field, onSave } = renderBox();
    await userEvent.click(field);
    await userEvent.click(within(box).getByText("pips (1 pip = 0.10)"));
    expect(onSave).not.toHaveBeenCalledWith("lk_entry_realignment", expect.anything());
  });
});
