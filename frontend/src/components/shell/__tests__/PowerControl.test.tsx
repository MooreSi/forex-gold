import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PowerControl } from "../PowerControl";

/**
 * Restart and Stop, restored from the NiceGUI header's power button.
 *
 * Neither closes a position — open trades keep running to their own SL/TP on
 * the broker's side, the same as when the machine is turned off — but both
 * end the process that is managing them, so neither happens on one press.
 *
 * Nothing here restarts anything: `fetch` is a recorder.
 */
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async () => ({
    ok: true, status: 200, json: async () => ({ result: "done" }),
  }));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const posts = () => fetchMock.mock.calls.filter((c) => c[1]?.method === "POST");

describe("PowerControl", () => {
  it("sends nothing on the first press", async () => {
    render(<PowerControl />);

    await userEvent.click(screen.getByTestId("power-control"));

    expect(posts()).toHaveLength(0);
  });

  it("restarts through the node endpoint", async () => {
    render(<PowerControl />);
    await userEvent.click(screen.getByTestId("power-control"));

    await userEvent.click(screen.getByRole("button", { name: /Restart/ }));

    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0][0]).toBe("/api/node/restart");
  });

  it("stops through a different endpoint from restart", async () => {
    // One press apart in the same dialog. Sending a restart for a stop leaves
    // the app running when the operator believes it is off.
    render(<PowerControl />);
    await userEvent.click(screen.getByTestId("power-control"));

    await userEvent.click(screen.getByRole("button", { name: /Stop/ }));

    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0][0]).toBe("/api/node/stop");
  });

  it("says that stopping does not come back on its own", async () => {
    render(<PowerControl />);

    await userEvent.click(screen.getByTestId("power-control"));

    expect(screen.getByText(/does not start itself again/i)).toBeInTheDocument();
  });

  it("says open positions keep running at the broker", async () => {
    // The question an operator actually has before pressing either of these.
    render(<PowerControl />);

    await userEvent.click(screen.getByTestId("power-control"));

    expect(screen.getByText(/own SL\/TP/)).toBeInTheDocument();
  });

  it("cancelling sends nothing", async () => {
    render(<PowerControl />);
    await userEvent.click(screen.getByTestId("power-control"));

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(posts()).toHaveLength(0);
  });

  it("reports a refusal in the backend's own words", async () => {
    fetchMock.mockImplementation(async () => ({
      ok: false, status: 400,
      json: async () => ({ error: { kind: "refusal", message: "Not permitted here.", ref: null } }),
    }));
    render(<PowerControl />);
    await userEvent.click(screen.getByTestId("power-control"));

    await userEvent.click(screen.getByRole("button", { name: /Restart/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Not permitted here.");
  });
});
