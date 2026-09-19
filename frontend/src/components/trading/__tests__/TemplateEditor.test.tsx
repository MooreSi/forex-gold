import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { TemplateEditor } from "../internal/TemplateEditor";

/**
 * The EA template form that replaced a hundred-key JSON textarea.
 *
 * The owner, 2026-09-19: "i'm unable to edit a template". The editor was a
 * ten-row textarea holding the whole template as raw JSON — unlabelled,
 * ungrouped, and with no idea which fields are booleans or which are enums
 * with a fixed set of values.
 *
 * Three properties matter here and each is a way the old editor failed:
 * every field the backend accepts is reachable, a field's type decides its
 * control, and a save sends what the backend asked for rather than whatever
 * the operator managed to type.
 */
const SCHEMA = [
  { name: "mode", type: "choice", default: "single", choices: ["single", "grid"] },
  { name: "sl_pips", type: "number", default: 50, choices: [] },
  { name: "anchors", type: "integer", default: 1, choices: [] },
  { name: "auto_sl", type: "boolean", default: true, choices: [] },
  { name: "tp1_pips", type: "number", default: 40, choices: [] },
  { name: "trail_mode", type: "choice", default: "off",
    choices: ["off", "step", "candle"] },
  // Deliberately not mentioned in templateGroups.ts.
  { name: "some_new_backend_field", type: "number", default: 7, choices: [] },
];

const VALUES = {
  name: "Asian - Single", mode: "single", sl_pips: 50, anchors: 1,
  auto_sl: true, tp1_pips: 40, trail_mode: "step",
  some_new_backend_field: 7, created_at: 1787864397, updated_at: 1789729673,
};

function renderEditor(over: Record<string, unknown> = {}) {
  const onSave = vi.fn(
    async (_name: string, _values: Record<string, unknown>) => ({ pushed: false }),
  );
  render(
    <TemplateEditor
      name="Asian - Single"
      values={{ ...VALUES, ...over }}
      schema={SCHEMA}
      onSave={onSave}
      onClose={() => {}}
    />,
  );
  return { onSave };
}

describe("every field is reachable", () => {
  it("shows a field this file has never heard of", async () => {
    // The rule that keeps a newly added backend field editable without a
    // matching change on this side.
    renderEditor();

    expect(screen.getByLabelText(/some new backend field/i)).toBeInTheDocument();
  });

  it("groups the fields it does know about", async () => {
    renderEditor();

    expect(screen.getByText("Entry and lots")).toBeInTheDocument();
    expect(screen.getByText("Stop loss")).toBeInTheDocument();
  });

  it("does not offer the bookkeeping as a setting", async () => {
    // name/created_at/updated_at are not in the schema, so they must not
    // appear as editable fields.
    renderEditor();

    expect(screen.queryByLabelText(/created at/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/updated at/i)).not.toBeInTheDocument();
  });
});

describe("the control matches the type", () => {
  it("renders a switch for a boolean", async () => {
    renderEditor();

    expect(screen.getByLabelText(/Set a stop when the signal has none/i))
      .toHaveAttribute("type", "checkbox");
  });

  it("renders a menu of the allowed values for a choice", async () => {
    renderEditor();
    const select = screen.getByLabelText(/Trail type/i);

    expect(within(select).getAllByRole("option").map((o) => o.textContent))
      .toEqual(["off", "step", "candle"]);
  });

  it("shows the unit, because 50 means four different things", async () => {
    renderEditor();

    expect(screen.getByTestId("unit-sl_pips")).toHaveTextContent("pips");
  });
});

describe("saving", () => {
  it("sends every field, not only the ones that were touched", async () => {
    // A partial save would be read by the backend as "the rest are defaults",
    // which silently resets a tuned template.
    const { onSave } = renderEditor();

    await userEvent.click(screen.getByRole("button", { name: /Save/ }));

    const sent = onSave.mock.calls[0]![1];
    for (const field of SCHEMA) {
      expect(sent).toHaveProperty(field.name);
    }
  });

  it("sends an edited number as a number, not as a string", async () => {
    const { onSave } = renderEditor();
    const input = screen.getByLabelText(/Stop distance/i);

    await userEvent.clear(input);
    await userEvent.type(input, "75");
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));

    expect(onSave.mock.calls[0]![1].sl_pips).toBe(75);
  });

  it("sends a toggled switch as a boolean", async () => {
    const { onSave } = renderEditor();

    await userEvent.click(screen.getByLabelText(/Set a stop when the signal has none/i));
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));

    expect(onSave.mock.calls[0]![1].auto_sl).toBe(false);
  });

  it("does not send the bookkeeping back", async () => {
    // created_at/updated_at are the service's, and name is the key.
    const { onSave } = renderEditor();

    await userEvent.click(screen.getByRole("button", { name: /Save/ }));

    expect(onSave.mock.calls[0]![1]).not.toHaveProperty("created_at");
    expect(onSave.mock.calls[0]![1]).not.toHaveProperty("updated_at");
  });

  it("keeps a half-typed decimal instead of swallowing the point", async () => {
    // `Number("1.")` is 1, so re-rendering from the number drops the point the
    // operator just typed. Same rule the backtest form documents.
    renderEditor();
    const input = screen.getByLabelText(/Stop distance/i);

    await userEvent.clear(input);
    await userEvent.type(input, "1.");

    expect(input).toHaveValue("1.");
  });

  it("reports what happened to the push, separately from the save", async () => {
    renderEditor();

    await userEvent.click(screen.getByRole("button", { name: /Save/ }));

    expect(await screen.findByRole("status")).toHaveTextContent(/next signal/i);
  });
});

describe("finding a field", () => {
  it("filters to the fields matching a search", async () => {
    renderEditor();

    await userEvent.type(screen.getByLabelText(/Search/i), "trail");

    expect(screen.getByLabelText(/Trail type/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Stop distance/i)).not.toBeInTheDocument();
  });

  it("says when a search matches nothing rather than showing a blank form", async () => {
    renderEditor();

    await userEvent.type(screen.getByLabelText(/Search/i), "zzzz");

    expect(screen.getByText(/No setting matches/i)).toBeInTheDocument();
  });
});

describe("a value it cannot represent", () => {
  it("offers a choice only as its allowed values", async () => {
    // The textarea let you save trail_mode: "stepp" and find out from a 500.
    renderEditor();
    const select = screen.getByLabelText(/Trail type/i);

    expect(select.tagName).toBe("SELECT");
    expect(within(select).queryByText("stepp")).not.toBeInTheDocument();
  });

  it("sends the schema default for a box left empty, never NaN", async () => {
    const { onSave } = renderEditor();

    await userEvent.clear(screen.getByLabelText(/Stop distance/i));
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));

    expect(onSave.mock.calls[0]![1].sl_pips).toBe(50);
  });

  it("rounds a decimal typed into an integer field", async () => {
    const { onSave } = renderEditor();
    const input = screen.getByLabelText(/Anchor legs/i);

    await userEvent.clear(input);
    await userEvent.type(input, "2.6");
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));

    expect(onSave.mock.calls[0]![1].anchors).toBe(3);
  });
});
