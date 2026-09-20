/**
 * A settings field must not throw away what is being typed into it.
 *
 * `SettingsField` re-syncs its draft from the stored value whenever that value
 * or the save counter changes. That is deliberate and load-bearing: a service
 * that clamps 99 back to 2 leaves `value` unchanged, so a field keyed on
 * `value` alone keeps "99" on screen and reads as a 99% risk setting the
 * engine is not using.
 *
 * But it re-synced unconditionally, including while the operator had the field
 * focused and half-edited. Open Trading > Risk and start typing before the
 * settings request resolves and your input is replaced by the stored number.
 *
 * It surfaced as a CI flake rather than a report: on a slow runner the load
 * landed between `clear()` and `type("2")`, so the field held "1", took the
 * "2" on the end, and saved **12% risk per trade** where the test asked for 2.
 * The same race on a real machine writes the same number.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SettingsField } from "../internal/SettingsField";

describe("SettingsField", () => {
  it("shows the stored value", () => {
    render(<SettingsField label="Risk per trade (%)" value="1" onCommit={vi.fn()} />);

    expect(screen.getByLabelText("Risk per trade (%)")).toHaveValue("1");
  });

  it("does not replace what is being typed when the stored value arrives", async () => {
    // The flake, made deterministic: the field is focused and edited, and
    // THEN the load resolves.
    const { rerender } = render(
      <SettingsField label="Risk per trade (%)" value="" onCommit={vi.fn()} />,
    );
    const field = screen.getByLabelText("Risk per trade (%)");

    await userEvent.click(field);
    await userEvent.type(field, "2");
    rerender(<SettingsField label="Risk per trade (%)" value="1" onCommit={vi.fn()} />);

    expect(field).toHaveValue("2");
  });

  it("does not append to a value that arrives mid-edit", async () => {
    // Exactly the CI failure: "1" arriving after a clear, then "2" typed,
    // giving 12% risk per trade.
    const { rerender } = render(
      <SettingsField label="Risk per trade (%)" value="1" onCommit={vi.fn()} />,
    );
    const field = screen.getByLabelText("Risk per trade (%)");

    await userEvent.clear(field);
    rerender(<SettingsField label="Risk per trade (%)" value="1" onCommit={vi.fn()} />);
    await userEvent.type(field, "2");

    expect(field).toHaveValue("2");
  });

  it("adopts the stored value once the field is left", async () => {
    // The behaviour the re-sync exists for: the service clamped, and the box
    // must show what the engine will use, not what was typed.
    const onCommit = vi.fn();
    const { rerender } = render(
      <SettingsField label="Risk per trade (%)" value="1" version={0} onCommit={onCommit} />,
    );
    const field = screen.getByLabelText("Risk per trade (%)");

    await userEvent.clear(field);
    await userEvent.type(field, "99");
    await userEvent.tab();

    expect(onCommit).toHaveBeenCalledWith("99");

    // The save came back clamped: same stored value, bumped version.
    rerender(<SettingsField label="Risk per trade (%)" value="1" version={1} onCommit={onCommit} />);

    expect(field).toHaveValue("1");
  });

  it("commits only when the value actually changed", async () => {
    const onCommit = vi.fn();
    render(<SettingsField label="Risk per trade (%)" value="1" onCommit={onCommit} />);
    const field = screen.getByLabelText("Risk per trade (%)");

    await userEvent.click(field);
    await userEvent.tab();

    expect(onCommit).not.toHaveBeenCalled();
  });
});
