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

  it("starts a NEW poll when the key changes", async () => {
    // The Chart tab's timeframe and the Analysis tab's window are both in the
    // key. The first version of this hook held the entry in a ref, so a key
    // change re-registered the OLD entry under the new key: no fetch, no
    // error, and a panel still showing the previous window's data. That is the
    // bug this test exists for.
    const first = vi.fn().mockResolvedValue("7 days");
    const second = vi.fn().mockResolvedValue("365 days");

    const { result, rerender } = renderHook(
      ({ key, fetcher }) => usePoll(key, fetcher, 5000),
      { initialProps: { key: "window:7", fetcher: first } },
    );
    await waitFor(() => expect(result.current.data).toBe("7 days"));

    rerender({ key: "window:365", fetcher: second });

    await waitFor(() => expect(result.current.data).toBe("365 days"));
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("returns to the cached entry when the key changes back", async () => {
    // And the other half: the registry is a cache, so going back to a window
    // already loaded shows it immediately rather than blanking the panel.
    const first = vi.fn().mockResolvedValue("7 days");
    const second = vi.fn().mockResolvedValue("365 days");

    const { result, rerender } = renderHook(
      ({ key, fetcher }) => usePoll(key, fetcher, 5000),
      { initialProps: { key: "back:7", fetcher: first } },
    );
    await waitFor(() => expect(result.current.data).toBe("7 days"));
    rerender({ key: "back:365", fetcher: second });
    await waitFor(() => expect(result.current.data).toBe("365 days"));

    rerender({ key: "back:7", fetcher: first });

    expect(result.current.data).toBe("7 days");
    expect(first).toHaveBeenCalledTimes(1);
  });

  it("adopts the SHORTEST interval any live subscriber asked for", async () => {
    // The interval used to be fixed when the entry was created, so whichever
    // tab mounted FIRST decided the cadence for every later one. Visiting
    // Trading (5s) and then the Dashboard (3s) left the Dashboard on 5s, and
    // visiting them the other way round quietly sped Trading up -- neither
    // visible anywhere. A screen that asks for a faster refresh has to get
    // one, or asking is theatre.
    const fetcher = vi.fn().mockResolvedValue(1);
    const slow = renderHook(() => usePoll("cadence", fetcher, 5000));
    await waitFor(() => expect(slow.result.current.data).toBe(1));

    renderHook(() => usePoll("cadence", fetcher, 1000));
    fetcher.mockClear();

    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("goes back to the slower interval when the fast subscriber leaves", async () => {
    // Otherwise one visit to a fast screen speeds an endpoint up for the rest
    // of the session. This one reaches the MT5 bridge, and an uncached read
    // polled harder than anybody asked for is how bugs/030 stalled the event
    // loop.
    const fetcher = vi.fn().mockResolvedValue(1);
    renderHook(() => usePoll("cadence-back", fetcher, 4000));
    const fast = renderHook(() => usePoll("cadence-back", fetcher, 1000));
    await waitFor(() => expect(fast.result.current.data).toBe(1));

    fast.unmount();
    fetcher.mockClear();

    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(fetcher).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(fetcher).toHaveBeenCalledTimes(1);
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
