import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetPolls, usePoll } from "../usePoll";

beforeEach(() => {
  resetPolls();
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  resetPolls();
  vi.useRealTimers();
});

describe("usePoll", () => {
  it("populates before the first tick rather than after one interval", async () => {
    const fetcher = vi.fn().mockResolvedValue({ n: 1 });
    const { result } = renderHook(() => usePoll("k", fetcher, 5000));
    await waitFor(() => expect(result.current.data).toEqual({ n: 1 }));
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("serves two subscribers from ONE interval", async () => {
    // The whole reason this hook exists. Ten components polling their own
    // endpoints is what the NiceGUI timers cost.
    const fetcher = vi.fn().mockResolvedValue(1);
    const a = renderHook(() => usePoll("shared", fetcher, 1000));
    const b = renderHook(() => usePoll("shared", fetcher, 1000));
    await waitFor(() => expect(a.result.current.data).toBe(1));
    expect(b.result.current.data).toBe(1);

    fetcher.mockClear();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("starts a second interval for a DIFFERENT key", async () => {
    // Negative control for the test above: if the registry collapsed every
    // key into one entry, that test would pass for the wrong reason.
    const one = vi.fn().mockResolvedValue(1);
    const two = vi.fn().mockResolvedValue(2);
    renderHook(() => usePoll("one", one, 1000));
    renderHook(() => usePoll("two", two, 1000));
    await waitFor(() => expect(one).toHaveBeenCalled());
    await waitFor(() => expect(two).toHaveBeenCalled());
  });

  it("does not start a second request while one is still in flight", async () => {
    let release: (v: unknown) => void = () => {};
    const fetcher = vi.fn(() => new Promise((r) => { release = r; }));
    renderHook(() => usePoll("slow", fetcher, 100));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));

    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(fetcher).toHaveBeenCalledTimes(1);

    await act(async () => { release(1); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("pauses while the document is hidden", async () => {
    const fetcher = vi.fn().mockResolvedValue(1);
    renderHook(() => usePoll("hidden", fetcher, 100));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));

    const spy = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    fetcher.mockClear();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(fetcher).not.toHaveBeenCalled();

    spy.mockReturnValue(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(fetcher).toHaveBeenCalled();
  });

  it("keeps the last good data when a fetch fails, and records the error", async () => {
    // A transient failure should show a stale number flagged as stale, not
    // blank the panel. `updatedAt` is what makes the staleness visible.
    const fetcher = vi.fn()
      .mockResolvedValueOnce("good")
      .mockRejectedValue(new Error("network down"));
    const { result } = renderHook(() => usePoll("flaky", fetcher, 100));
    await waitFor(() => expect(result.current.data).toBe("good"));
    const firstUpdate = result.current.updatedAt;

    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.data).toBe("good");
    expect(result.current.updatedAt).toBe(firstUpdate);
  });

  it("stops the interval when the last subscriber unmounts", async () => {
    const fetcher = vi.fn().mockResolvedValue(1);
    const { unmount } = renderHook(() => usePoll("lonely", fetcher, 100));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    unmount();
    fetcher.mockClear();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(fetcher).not.toHaveBeenCalled();
  });
});
