import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FeedbackDialog } from "../FeedbackDialog";
import { resetPolls } from "@/hooks/usePoll";

/**
 * The Feedback popup's optional email, so the owner can reply directly.
 * Behind an "Add your email" link, so the form stays one box for someone who
 * does not want a reply. Sent only when typed; the backend checks its shape
 * and says why if not.
 */

const fetchMock = vi.fn();
beforeEach(() => {
  resetPolls();
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => ({ ok: true, id: "x" }) });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const posts = () =>
  fetchMock.mock.calls.filter(([url]) => String(url) === "/api/feedback");

describe("the reply email", () => {
  it("is offered and optional", async () => {
    const user = userEvent.setup();
    render(<FeedbackDialog open onOpenChange={() => {}} />);
    expect(screen.queryByRole("textbox", { name: /email/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /add your email/i }));

    const field = screen.getByRole("textbox", { name: /email/i });
    expect(field).toHaveAttribute("type", "email");
    expect(field).not.toBeRequired();
  });

  it("is sent with the feedback when typed", async () => {
    const user = userEvent.setup();
    render(<FeedbackDialog open onOpenChange={() => {}} />);

    await user.type(screen.getByRole("textbox", { name: /your feedback/i }), "It froze");
    await user.click(screen.getByRole("button", { name: /add your email/i }));
    await user.type(screen.getByRole("textbox", { name: /email/i }), "sam@reply.co");
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(JSON.parse(posts()[0][1].body)).toEqual({
      kind: "feature", message: "It froze", email: "sam@reply.co",
    });
  });

  it("shows the backend's reason when the address is refused", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValue({
      ok: false, status: 400,
      json: async () => ({ error: { kind: "refusal",
        message: "That email address does not look right.", ref: null } }),
    });
    render(<FeedbackDialog open onOpenChange={() => {}} />);

    await user.type(screen.getByRole("textbox", { name: /your feedback/i }), "hi");
    await user.click(screen.getByRole("button", { name: /add your email/i }));
    await user.type(screen.getByRole("textbox", { name: /email/i }), "sam@x");
    await user.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByText(/email address does not look right/i)).toBeInTheDocument();
  });
});
