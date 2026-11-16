import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FeedbackCompletedNotice } from "../FeedbackCompletedNotice";
import { resetPolls } from "@/hooks/usePoll";

/**
 * The popup the sender sees once the owner marks their feedback Completed in
 * the admin console. The notices ride the shared header poll; Close posts
 * back that it was seen, so it shows once.
 */

const NOTICE = {
  id: "a".repeat(32), kind: "feature", message: "Dark mode please",
  completed_at: 1700000000,
};

const fetchMock = vi.fn();
let notices: unknown[] | undefined;

beforeEach(() => {
  resetPolls();
  notices = [NOTICE];
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (url: string) => {
    if (String(url) === "/api/system/header") {
      return { ok: true, status: 200, json: async () => ({ feedback_done: notices }) };
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const seenPosts = () =>
  fetchMock.mock.calls.filter(([url]) => String(url).includes("/api/feedback/notices/"));

describe("the Feedback completed popup", () => {
  it("says the feedback was noted and completed, and which one", async () => {
    render(<FeedbackCompletedNotice />);

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("Feedback noted and completed")).toBeInTheDocument();
    expect(screen.getByText(/dark mode please/i)).toBeInTheDocument();
    expect(screen.getByText(/feature request/i)).toBeInTheDocument();
  });

  it("shows nothing when nothing is completed", async () => {
    notices = [];
    render(<FeedbackCompletedNotice />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows nothing when the header has no such field", async () => {
    notices = undefined;
    render(<FeedbackCompletedNotice />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("marks it seen on Close and does not show it again", async () => {
    const user = userEvent.setup();
    render(<FeedbackCompletedNotice />);
    await screen.findByRole("dialog");

    notices = [];
    await user.click(screen.getAllByRole("button", { name: /close/i })[0]);

    await waitFor(() => expect(seenPosts()).toHaveLength(1));
    expect(String(seenPosts()[0][0])).toBe(`/api/feedback/notices/${NOTICE.id}/seen`);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});
