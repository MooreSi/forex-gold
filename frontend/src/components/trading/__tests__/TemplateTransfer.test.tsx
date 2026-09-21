import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TemplateTransfer } from "../internal/TemplateTransfer";

/**
 * Import and Export for EA templates.
 *
 * Owner, 2026-09-21: "trading > ea template - needs buttons to import,
 * export and delete ea templates". Delete was already there; these two were
 * in the NiceGUI app (`frontend/pages/trading/_ea_templates.py`) and were
 * never ported, so a tuned set of templates could not leave the machine it
 * was tuned on.
 *
 * Two properties matter, and both are about not losing work:
 *
 * - **Import never overwrites unless the operator ticks it.** A file from
 *   another machine silently replacing a locally tuned template is the
 *   failure this feature could cause, so the default is the safe one and the
 *   screen reports what it skipped.
 * - **A refusal reads as a refusal.** Picking the wrong file is an ordinary
 *   mistake. It has to say which file and why, not "Request failed".
 */
const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  // jsdom has no download plumbing; the anchor click is what we assert on.
  vi.stubGlobal("URL", {
    ...URL,
    createObjectURL: vi.fn(() => "blob:fake"),
    revokeObjectURL: vi.fn(),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function answer(body: unknown, ok = true, status = 200) {
  fetchMock.mockResolvedValueOnce({
    ok, status,
    headers: { get: () => "application/json" },
    json: async () => body,
    text: async () => JSON.stringify(body),
  });
}

function renderTransfer(onImported = vi.fn()) {
  render(<TemplateTransfer onImported={onImported} />);
  return { onImported };
}

describe("exporting", () => {
  it("asks the backend for the file and saves it under the name it gave", async () => {
    answer({ content: '{"format":"forex-ea-templates"}',
             filename: "ea_templates_20260921_101500.eatpl.json" });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});
    renderTransfer();

    await userEvent.click(screen.getByRole("button", { name: /export/i }));

    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(fetchMock.mock.calls[0][0]).toBe("/api/trading/templates/export");
  });

  it("says so when the export could not be read, rather than failing silently", async () => {
    answer({ error: { kind: "failure", message: "the database is locked" } }, false, 500);
    renderTransfer();

    await userEvent.click(screen.getByRole("button", { name: /export/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/database is locked/);
  });
});

describe("importing", () => {
  const file = () =>
    new File(['{"format":"forex-ea-templates","templates":[]}'],
             "shared.eatpl.json", { type: "application/json" });

  it("sends the file's text and does not overwrite by default", async () => {
    answer({ added: ["Asian - Grid"], replaced: [], skipped: [] });
    renderTransfer();

    await userEvent.upload(screen.getByLabelText(/template file/i), file());
    await userEvent.click(screen.getByRole("button", { name: /^import$/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.overwrite).toBe(false);
    expect(body.content).toContain("forex-ea-templates");
  });

  it("overwrites only when the operator ticks it", async () => {
    answer({ added: [], replaced: ["Asian - Grid"], skipped: [] });
    renderTransfer();

    await userEvent.upload(screen.getByLabelText(/template file/i), file());
    await userEvent.click(screen.getByLabelText(/replace/i));
    await userEvent.click(screen.getByRole("button", { name: /^import$/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).overwrite).toBe(true);
  });

  it("names what it skipped, and says how to take it", async () => {
    // Silence here reads as "nothing in the file", which is the wrong
    // conclusion and the reason the skip list is reported at all.
    answer({ added: ["New One"], replaced: [], skipped: ["Asian - Grid"] });
    renderTransfer();

    await userEvent.upload(screen.getByLabelText(/template file/i), file());
    await userEvent.click(screen.getByRole("button", { name: /^import$/i }));

    const said = await screen.findByRole("status");
    expect(said).toHaveTextContent(/Asian - Grid/);
    expect(said).toHaveTextContent(/replace/i);
  });

  it("tells the page to reload the list once something landed", async () => {
    answer({ added: ["New One"], replaced: [], skipped: [] });
    const { onImported } = renderTransfer();

    await userEvent.upload(screen.getByLabelText(/template file/i), file());
    await userEvent.click(screen.getByRole("button", { name: /^import$/i }));

    await waitFor(() => expect(onImported).toHaveBeenCalled());
  });

  it("shows a refused file's own reason", async () => {
    answer({ error: { kind: "refusal",
                      message: "That file is not an EA template export." } }, false, 409);
    renderTransfer();

    await userEvent.upload(screen.getByLabelText(/template file/i), file());
    await userEvent.click(screen.getByRole("button", { name: /^import$/i }));

    expect(await screen.findByRole("alert"))
      .toHaveTextContent(/not an EA template export/);
  });

  it("will not send with no file chosen", async () => {
    renderTransfer();

    expect(screen.getByRole("button", { name: /^import$/i })).toBeDisabled();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
