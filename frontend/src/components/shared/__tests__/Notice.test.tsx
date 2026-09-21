import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Notice } from "../Notice";

/**
 * The header's two "it worked" lines used to be permanent.
 *
 * Reported 2026-09-21: after pressing Restart, "the wording 'Restarting app in
 * 5 seconds — reconnect your browser shortly.' never goes away after it has
 * restarted". Nothing ever cleared it. The same was true of the account
 * switch's note beside it.
 *
 * The normal way out is the page reloading itself when the backend comes back.
 * The timeout here is for when it does NOT come back — Stop, or a restart that
 * failed — which is exactly when a stale line is most misleading.
 */
afterEach(() => vi.useRealTimers());

describe("Notice", () => {
  it("shows what happened", () => {
    render(<Notice onDismiss={() => {}}>Restarting app in 5 seconds</Notice>);

    expect(screen.getByRole("status")).toHaveTextContent("Restarting app in 5 seconds");
  });

  it("is an alert, not a status, when it is bad news", () => {
    render(<Notice tone="bad" onDismiss={() => {}}>Could not restart</Notice>);

    expect(screen.getByRole("alert")).toHaveTextContent("Could not restart");
  });

  it("can be dismissed by hand", async () => {
    const dismissed = vi.fn();
    render(<Notice onDismiss={dismissed}>Restarting</Notice>);

    await userEvent.click(screen.getByRole("button", { name: /dismiss/i }));

    expect(dismissed).toHaveBeenCalled();
  });

  it("clears itself when nothing else does", () => {
    vi.useFakeTimers();
    const dismissed = vi.fn();
    render(<Notice ttlMs={1000} onDismiss={dismissed}>Restarting</Notice>);

    act(() => vi.advanceTimersByTime(1500));

    expect(dismissed).toHaveBeenCalled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("is still there a moment before that", () => {
    vi.useFakeTimers();
    render(<Notice ttlMs={1000} onDismiss={() => {}}>Restarting</Notice>);

    act(() => vi.advanceTimersByTime(900));

    expect(screen.getByRole("status")).toBeInTheDocument();
  });
});

describe("the countdown", () => {
  it("is not restarted by the parent re-rendering", () => {
    // Both parents poll, so they re-render every few seconds. A countdown
    // keyed on `children` — a fresh object each render — would be reset every
    // time and the notice would never expire, which is the whole bug.
    vi.useFakeTimers();
    const dismissed = vi.fn();
    const { rerender } = render(
      <Notice ttlMs={1000} onDismiss={dismissed}>Restarting</Notice>,
    );

    act(() => vi.advanceTimersByTime(600));
    rerender(<Notice ttlMs={1000} onDismiss={dismissed}>Restarting</Notice>);
    act(() => vi.advanceTimersByTime(600));

    expect(dismissed).toHaveBeenCalled();
  });
});
