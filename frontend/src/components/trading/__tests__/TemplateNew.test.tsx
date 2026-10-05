/**
 * Creating an EA template.
 *
 * Templates can be added, so the panel needs a way to add one -- including
 * when there are none yet, which is exactly when the list is empty and has
 * nothing else to click. A new template is saved under the typed name with no
 * overrides (every field at its default), then opened for editing.
 *
 * Nothing here reaches a backend: `fetch` is a recorder.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TemplatesSection } from "../internal/TemplatesSection";

const TEMPLATES = [{ name: "Grid Runner", sl_pips: 50 }];

function renderSection(over: Record<string, unknown> = {}) {
  const onSave = vi.fn(async () => ({ pushed: false }));
  render(
    <TemplatesSection
      templates={TEMPLATES}
      eaConnected
      eaLastSeen={2}
      onSave={onSave}
      onDelete={vi.fn()}
      onInstallBuiltin={vi.fn()}
      onImported={vi.fn()}
      onRename={vi.fn(async () => ({ repointed: {} }))}
      {...over}
    />,
  );
  return onSave;
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => ({
    ok: true, status: 200,
    json: async () => (String(url).includes("/schema") ? { fields: [] } : {}),
  })));
});
afterEach(() => vi.unstubAllGlobals());

describe("the New template control", () => {
  it("is offered with no template selected", () => {
    renderSection();
    expect(screen.getByRole("button", { name: /^New template$/i })).toBeInTheDocument();
  });

  it("is offered when there are no templates at all", () => {
    renderSection({ templates: [] });
    expect(screen.getByRole("button", { name: /^New template$/i })).toBeInTheDocument();
  });

  it("saves the typed name with no overrides", async () => {
    const user = userEvent.setup();
    const onSave = renderSection();

    await user.click(screen.getByRole("button", { name: /^New template$/i }));
    await user.type(screen.getByLabelText("Template name"), "50/50");
    await user.click(screen.getByRole("button", { name: /^Create$/i }));

    expect(onSave).toHaveBeenCalledWith("50/50", {});
  });

  it("refuses a name that already exists rather than overwriting it", async () => {
    const user = userEvent.setup();
    const onSave = renderSection();

    await user.click(screen.getByRole("button", { name: /^New template$/i }));
    await user.type(screen.getByLabelText("Template name"), "grid runner");

    expect(screen.getByRole("button", { name: /^Create$/i })).toBeDisabled();
    expect(onSave).not.toHaveBeenCalled();
  });
});
