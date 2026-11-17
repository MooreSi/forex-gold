import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppHeader } from "../AppHeader";
import { AuthProvider } from "@/contexts/AuthContext";
import { resetPolls } from "@/hooks/usePoll";

/**
 * The admin node's "new feedback" alert in the title bar.
 *
 * Only where the licence admin console is mounted. The count rides the header
 * poll; the entries are fetched when the alert is clicked, and reading them
 * marks those ids read so the alert goes away.
 */

const ROW = {
  id: "a".repeat(32), kind: "bug", message: "The chart froze", name: "Sam",
  email: "s@x.co", hostname: "mac-01", version: "1.2.3", submitted_at: 1700000000,
};

let unread: number;
let adminAvailable: boolean;
const fetchMock = vi.fn();

beforeEach(() => {
  resetPolls();
  unread = 2;
  adminAvailable = true;
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (url: string) => {
    const u = String(url);
    let json: unknown = { ok: true };
    if (u.includes("/api/admin/status")) {
      return adminAvailable
        ? { ok: true, status: 200, json: async () => ({ available: true, can_sign: true }) }
        : { ok: false, status: 404, json: async () => ({}) };
    }
    if (u === "/api/system/header") {
      json = {
        account: null, bridge: null, tick: null, active_trader: null,
        pause: { paused: false, reason: "", until: null, source: "" },
        remote_connected: false, ea_badge: null, feedback_unread: unread,
      };
    } else if (u.includes("status-badge")) {
      json = { state: "ok", label: "Trading Active", detail: "", until: null,
               resume_ts: null, can_resume: false };
    } else if (u === "/api/feedback/inbox") {
      json = [ROW];
    } else if (u === "/api/feedback/inbox/read") {
      unread = 0;   // what the backend does: the count drops once marked read
      json = { ok: true, marked: 1 };
    }
    return { ok: true, status: 200, json: async () => json };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const renderHeader = () => render(<AuthProvider><AppHeader /></AuthProvider>);
const reads = () =>
  fetchMock.mock.calls.filter(([url]) => String(url) === "/api/feedback/inbox/read");

describe("the new-feedback alert", () => {
  it("shows the count on the admin node", async () => {
    renderHeader();

    expect(await screen.findByRole("button", { name: /2 new feedback/i })).toBeInTheDocument();
  });

  it("is absent where the admin console is not mounted", async () => {
    adminAvailable = false;
    renderHeader();

    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([u]) => String(u) === "/api/system/header")).toBe(true));
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([u]) => String(u).includes("/api/admin/status"))).toBe(true));
    expect(screen.queryByRole("button", { name: /new feedback/i })).not.toBeInTheDocument();
  });

  it("is absent with nothing unread", async () => {
    unread = 0;
    renderHeader();

    await screen.findByRole("link", { name: "Licence admin" });
    expect(screen.queryByRole("button", { name: /new feedback/i })).not.toBeInTheDocument();
  });

  it("opens the feedback and marks exactly those ids read", async () => {
    const user = userEvent.setup();
    renderHeader();

    await user.click(await screen.findByRole("button", { name: /2 new feedback/i }));

    expect(await screen.findByText("The chart froze")).toBeInTheDocument();
    expect(screen.getByText(/sam/i)).toBeInTheDocument();
    await waitFor(() => expect(reads()).toHaveLength(1));
    expect(JSON.parse(reads()[0][1].body)).toEqual({ ids: [ROW.id] });
  });

  it("goes away once read", async () => {
    const user = userEvent.setup();
    renderHeader();

    await user.click(await screen.findByRole("button", { name: /2 new feedback/i }));
    await waitFor(() => expect(reads()).toHaveLength(1));
    await user.click(screen.getAllByRole("button", { name: /close/i })[0]);

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /new feedback/i })).not.toBeInTheDocument());
  });
});
