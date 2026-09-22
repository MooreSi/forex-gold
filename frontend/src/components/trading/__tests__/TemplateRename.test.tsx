/**
 * Renaming an EA template.
 *
 * The name is a foreign key nobody declared: a strategy override is stored as
 * the string `template:<name>` in the trading schedule, on a channel, in the
 * AI's recommendation and in the global risk settings. The backend repoints
 * all of them; what this panel has to do is **say so**, before and after,
 * because a rename that quietly moves live trading configuration is exactly
 * the kind of change an operator needs to be able to see.
 *
 * Nothing here reaches a backend: `fetch` is a recorder.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TemplatesSection } from "../internal/TemplatesSection";

const TEMPLATES = [
  { name: "Grid Runner", sl_pips: 50 },
  { name: "Trail Runner", sl_pips: 60 },
];

let references: Record<string, number>;
let renameResponse: { status: number; body: unknown };
let fetchMock: ReturnType<typeof vi.fn>;

function renderSection(over: Record<string, unknown> = {}) {
  return render(
    <TemplatesSection
      templates={TEMPLATES}
      eaConnected
      eaLastSeen={2}
      onSave={vi.fn(async () => ({ pushed: true }))}
      onDelete={vi.fn()}
      onInstallBuiltin={vi.fn()}
      onImported={vi.fn()}
      onRename={vi.fn(async () => ({
        repointed: { schedule: 2, channel_assignments: 1,
                     ai_recommendations: 0, risk_settings: 1 },
      }))}
      {...over}
    />,
  );
}

beforeEach(() => {
  references = { schedule: 2, channel_assignments: 1, ai_recommendations: 0,
                 risk_settings: 1 };
  renameResponse = { status: 200, body: {} };
  fetchMock = vi.fn(async (url: string) => {
    if (String(url).includes("/references")) {
      return { ok: true, status: 200, json: async () => ({ references }) };
    }
    if (String(url).includes("/schema")) {
      return { ok: true, status: 200, json: async () => ({ fields: [] }) };
    }
    return { ok: renameResponse.status < 400, status: renameResponse.status,
             json: async () => renameResponse.body };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

async function openRename() {
  const user = userEvent.setup();
  renderSection();
  await user.click(within(await screen.findByTestId("template-Grid Runner"))
    .getByRole("button"));
  await user.click(screen.getByRole("button", { name: /^Rename Grid Runner$/i }));
  return user;
}

describe("the rename control", () => {
  it("is not offered until a template is selected", () => {
    renderSection();

    expect(screen.queryByRole("button", { name: /^Rename /i }))
      .not.toBeInTheDocument();
  });

  it("names the template it will rename", async () => {
    await openRename();

    expect(within(screen.getByRole("dialog")).getByText(/Grid Runner/))
      .toBeInTheDocument();
  });
});

describe("what it says before renaming", () => {
  it("warns that live configuration points at this template", async () => {
    // "2 schedule windows, 1 channel, the global strategy" — the operator is
    // about to move all of it, and the panel is the only place that can say
    // so before it happens.
    await openRename();

    const dialog = screen.getByRole("dialog");
    await waitFor(() =>
      expect(within(dialog).getByTestId("rename-references"))
        .toHaveTextContent(/2 schedule window/i));
    expect(within(dialog).getByTestId("rename-references"))
      .toHaveTextContent(/global strategy/i);
  });

  it("says plainly when nothing points at it", async () => {
    references = { schedule: 0, channel_assignments: 0, ai_recommendations: 0,
                   risk_settings: 0 };
    await openRename();

    await waitFor(() =>
      expect(within(screen.getByRole("dialog")).getByTestId("rename-references"))
        .toHaveTextContent(/nothing else refers to it/i));
  });
});

describe("renaming", () => {
  it("sends the new name and closes", async () => {
    const onRename = vi.fn(async () => ({
      repointed: { schedule: 2, channel_assignments: 1,
                   ai_recommendations: 0, risk_settings: 1 },
    }));
    const user = userEvent.setup();
    renderSection({ onRename });
    await user.click(within(await screen.findByTestId("template-Grid Runner"))
    .getByRole("button"));
    await user.click(screen.getByRole("button", { name: /^Rename Grid Runner$/i }));

    await user.clear(screen.getByLabelText(/New name/i));
    await user.type(screen.getByLabelText(/New name/i), "Grid Runner v2");
    await user.click(screen.getByRole("button", { name: /^Rename$/i }));

    await waitFor(() =>
      expect(onRename).toHaveBeenCalledWith("Grid Runner", "Grid Runner v2"));
  });

  it("will not submit an unchanged or empty name", async () => {
    const onRename = vi.fn();
    const user = userEvent.setup();
    renderSection({ onRename });
    await user.click(within(await screen.findByTestId("template-Grid Runner"))
    .getByRole("button"));
    await user.click(screen.getByRole("button", { name: /^Rename Grid Runner$/i }));

    // The box opens holding the current name, so the confirm starts disabled.
    expect(screen.getByRole("button", { name: /^Rename$/i })).toBeDisabled();

    await user.clear(screen.getByLabelText(/New name/i));
    expect(screen.getByRole("button", { name: /^Rename$/i })).toBeDisabled();
    expect(onRename).not.toHaveBeenCalled();
  });

  it("shows the backend's refusal rather than closing on it", async () => {
    const onRename = vi.fn(async () => {
      throw new Error("There is already an EA template called 'Trail Runner'.");
    });
    const user = userEvent.setup();
    renderSection({ onRename });
    await user.click(within(await screen.findByTestId("template-Grid Runner"))
    .getByRole("button"));
    await user.click(screen.getByRole("button", { name: /^Rename Grid Runner$/i }));
    await user.clear(screen.getByLabelText(/New name/i));
    await user.type(screen.getByLabelText(/New name/i), "Trail Runner");

    await user.click(screen.getByRole("button", { name: /^Rename$/i }));

    expect(await screen.findByRole("alert"))
      .toHaveTextContent(/already an EA template/i);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
