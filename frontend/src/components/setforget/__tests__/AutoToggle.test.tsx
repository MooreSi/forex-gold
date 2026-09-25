/**
 * Set & Forget > Auto. The button switches the backend's 15-minute long scan
 * on and off; the scan itself (and every refusal in front of its orders) is
 * server-side. What this page owes the operator is: the switch says whether
 * Auto is on, pressing it sends exactly that, and the last scan's decision is
 * shown beside it so "is it doing anything?" has an answer.
 *
 * Nothing here reaches a broker: `fetch` is a recorder.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AutoStatusLine, AutoToggle } from "../internal/AutoToggle";
import { resetPolls } from "@/hooks/usePoll";

let status: Record<string, unknown>;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  resetPolls();
  status = { enabled: false, decision: null, reason: "", last_run: null,
             interval_s: 900, max_trades_per_day: 2 };
  fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      status = { ...status, enabled: JSON.parse(String(init.body)).enabled };
    }
    return { ok: true, status: 200, json: async () => status };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { resetPolls(); vi.unstubAllGlobals(); });

const puts = () => fetchMock.mock.calls.filter(
  ([, init]) => (init as RequestInit | undefined)?.method === "PUT");

describe("the Auto switch", () => {
  it("shows Auto as off until the backend says it is on", async () => {
    render(<AutoToggle />);

    const button = await screen.findByRole("button", { name: /Auto/ });
    await waitFor(() => expect(button).toHaveAttribute("aria-pressed", "false"));
  });

  it("switches Auto on with one request naming the new state", async () => {
    render(<AutoToggle />);
    const button = await screen.findByRole("button", { name: /Auto/ });

    await userEvent.click(button);

    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(String(puts()[0][0])).toBe("/api/trading/setforget/auto");
    expect(JSON.parse(String((puts()[0][1] as RequestInit).body)))
      .toEqual({ enabled: true });
    // Re-queried: the tooltip wrapper remounts the button when its text
    // changes, so the element found before the click is detached by now.
    await waitFor(() => expect(screen.getByRole("button", { name: /Auto/ }))
      .toHaveAttribute("aria-pressed", "true"));
  });

  it("switches it off again from on", async () => {
    status.enabled = true;
    render(<AutoToggle />);
    await waitFor(() => expect(screen.getByRole("button", { name: /Auto/ }))
      .toHaveAttribute("aria-pressed", "true"));

    await userEvent.click(screen.getByRole("button", { name: /Auto/ }));

    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(JSON.parse(String((puts()[0][1] as RequestInit).body)))
      .toEqual({ enabled: false });
  });

  it("says what the last scan decided and why", async () => {
    status = { ...status, enabled: true, decision: "no_setup",
               reason: "Price has not reached the zone yet.",
               last_run: 1_750_000_000 };
    render(<><AutoToggle /><AutoStatusLine /></>);

    expect(await screen.findByText(/Price has not reached the zone yet\./))
      .toBeInTheDocument();
  });
});
