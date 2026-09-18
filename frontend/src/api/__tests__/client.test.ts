import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, onUnauthenticated } from "../client";

const json = (status: number, body: unknown, ok = status < 400) => ({
  ok, status, statusText: "", json: async () => body,
});

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("errors", () => {
  it("keeps a refusal's message intact and marks it as a refusal", async () => {
    fetchMock.mockResolvedValue(json(409, {
      error: { kind: "refusal", message: "Bridge not connected.", ref: null },
    }));
    await expect(api.post("/api/trading/orders/market")).rejects.toMatchObject({
      message: "Bridge not connected.",
      kind: "refusal",
    });
  });

  it("does not mark an internal error as a refusal", async () => {
    // Negative control. If everything read as a refusal, the UI would present
    // a crash to the user as if the backend had considered it and said no.
    fetchMock.mockResolvedValue(json(500, {
      error: { kind: "internal", message: "Unexpected.", ref: "ab12cd34" },
    }));
    try {
      await api.get("/api/trading/risk");
      throw new Error("should have thrown");
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      expect((e as ApiError).isRefusal).toBe(false);
      expect((e as ApiError).ref).toBe("ab12cd34");
    }
  });

  it("keeps the status line when the body is not JSON", async () => {
    // A non-JSON body usually means the request hit the static fallback rather
    // than a route. Replacing it with a friendly message hides that.
    fetchMock.mockResolvedValue({
      ok: false, status: 502, statusText: "Bad Gateway",
      json: async () => { throw new SyntaxError("not json"); },
    });
    await expect(api.get("/api/x")).rejects.toMatchObject({ message: "502 Bad Gateway" });
  });
});

describe("401", () => {
  it("notifies every listener so one call site cannot forget", async () => {
    const seen = vi.fn();
    const off = onUnauthenticated(seen);
    fetchMock.mockResolvedValue(json(401, {
      error: { kind: "unauthenticated", message: "Sign in to continue.", ref: null },
    }));
    await expect(api.get("/api/trading/risk")).rejects.toBeInstanceOf(ApiError);
    expect(seen).toHaveBeenCalledTimes(1);
    off();
  });

  it("does not notify on an ordinary failure", async () => {
    const seen = vi.fn();
    const off = onUnauthenticated(seen);
    fetchMock.mockResolvedValue(json(409, {
      error: { kind: "refusal", message: "no", ref: null },
    }));
    await expect(api.post("/api/x")).rejects.toBeInstanceOf(ApiError);
    expect(seen).not.toHaveBeenCalled();
    off();
  });
});

describe("requests", () => {
  it("sends the session cookie", async () => {
    fetchMock.mockResolvedValue(json(200, {}));
    await api.get("/api/system/version");
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: "same-origin" });
  });

  it("sets a JSON content type only when there is a body", async () => {
    fetchMock.mockResolvedValue(json(200, {}));
    await api.get("/api/system/version");
    expect(fetchMock.mock.calls[0][1].headers).toBeUndefined();
    await api.post("/api/x", { a: 1 });
    expect(fetchMock.mock.calls[1][1].headers).toEqual({ "Content-Type": "application/json" });
  });
});
