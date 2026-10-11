import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import { resetPolls, usePoll } from "@/hooks/usePoll";
import { useSettingsResource } from "../useSettingsResource";

/**
 * A settings save must reach every screen polling the same URL.
 *
 * Owner, 2026-10-11: Max daily loss changed from 10% to 8% on the Trading
 * tab's Risk section. It was stored, and `/api/settings/risk` answered 8, but
 * the Dashboard's "Daily loss cap" still said 10%. The Dashboard had been
 * unmounted while Trading was open and came back to its cached read, which it
 * does not repeat for another 60 seconds.
 */
const URL = "/api/settings/risk";
let stored: Record<string, unknown>;

beforeEach(() => {
  resetPolls();
  stored = { max_daily_loss_pct: 10 };
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      stored = { ...stored, ...JSON.parse(String(init.body)) };
    }
    return { ok: true, status: 200, json: async () => stored };
  }));
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const pollRisk = () => usePoll<Record<string, unknown>>(
  URL, () => api.get<Record<string, unknown>>(URL), 60_000);

describe("useSettingsResource", () => {
  it("makes a screen polling the saved URL show the new value when it returns", async () => {
    const dashboard = renderHook(pollRisk);
    await waitFor(() => expect(dashboard.result.current.data?.max_daily_loss_pct).toBe(10));
    dashboard.unmount();                      // the Trading tab is opened

    const risk = renderHook(() => useSettingsResource<Record<string, unknown>>(URL));
    await waitFor(() => expect(risk.result.current.data).not.toBeNull());
    await act(() => risk.result.current.save({ max_daily_loss_pct: 8 }));
    risk.unmount();                           // back to the Dashboard

    const back = renderHook(pollRisk);
    await waitFor(() => expect(back.result.current.data?.max_daily_loss_pct).toBe(8));
  });

  it("does not touch a poll of a different URL", async () => {
    // Negative control: a save that refreshed every poll would pass the test
    // above and cost a burst of requests, the bridge ones included, per save.
    const other = vi.fn().mockResolvedValue({ n: 1 });
    const watcher = renderHook(() => usePoll("/api/positions", other, 60_000));
    await waitFor(() => expect(watcher.result.current.data).toEqual({ n: 1 }));

    const risk = renderHook(() => useSettingsResource<Record<string, unknown>>(URL));
    await waitFor(() => expect(risk.result.current.data).not.toBeNull());
    await act(() => risk.result.current.save({ max_daily_loss_pct: 8 }));

    expect(other).toHaveBeenCalledTimes(1);
  });
});
