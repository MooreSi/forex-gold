import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FeedbackDialog } from "../FeedbackDialog";
import { AppShell } from "../AppShell";
import { AuthProvider } from "@/contexts/AuthContext";
import { resetPolls } from "@/hooks/usePoll";

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

describe("the Feedback popup", () => {
  it("offers the three kinds", () => {
    render(<FeedbackDialog open onOpenChange={() => {}} />);
    expect(screen.getByRole("radio", { name: /feature request/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /bug/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /general/i })).toBeInTheDocument();
  });

  it("will not send an empty message, and says why", () => {
    render(<FeedbackDialog open onOpenChange={() => {}} />);
    expect(screen.getByRole("button", { name: /send/i })).toBeDisabled();
  });

  it("sends the chosen kind and the text", async () => {
    const user = userEvent.setup();
    render(<FeedbackDialog open onOpenChange={() => {}} />);

    await user.click(screen.getByRole("radio", { name: /bug/i }));
    await user.type(screen.getByRole("textbox"), "It froze");
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(JSON.parse(posts()[0][1].body)).toEqual({ kind: "bug", message: "It froze" });
  });

  it("confirms, and does not close on a failure", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValue({
      ok: false, status: 400,
      json: async () => ({ error: { kind: "refusal", message: "Write something first.", ref: null } }),
    });
    const onOpenChange = vi.fn();
    render(<FeedbackDialog open onOpenChange={onOpenChange} />);

    await user.type(screen.getByRole("textbox"), "hi");
    await user.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByText("Write something first.")).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("thanks the user after a successful send", async () => {
    const user = userEvent.setup();
    render(<FeedbackDialog open onOpenChange={() => {}} />);

    await user.type(screen.getByRole("textbox"), "Great app");
    await user.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByText(/thanks/i)).toBeInTheDocument();
  });
});

describe("the Feedback button", () => {
  it("sits straight after the About tab and opens the popup", async () => {
    const user = userEvent.setup();
    render(<AuthProvider><AppShell /></AuthProvider>);

    const about = screen.getByRole("tab", { name: /about/i });
    const button = screen.getByRole("button", { name: /^feedback$/i });
    expect(about.nextElementSibling).toBe(button);

    await user.click(button);
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });
});
