import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EnginesPanel } from "../EnginesPanel";
import { resetPolls } from "@/hooks/usePoll";
import type { EnginesState } from "../hooks/useEnginesController";

function state(over: Partial<EnginesState> = {}): EnginesState {
  return {
    engines: [
      { id: "breakout", label: "Breakout", running: true, built: true },
      { id: "reversal", label: "Reversal", running: false, built: true },
    ],
    settings: { re_min_adx: 22, htf_bias_asian_exempt: 0, htf_bias_gate_enabled: 0 },
    // The shape `pro_model.status()` actually returns.
    pro_model: {
      ready: false, auc: null, n: 0, reason: "not fitted", fitted_at: 0,
      corpus: { pos: 1926, neg: 8190, wins: 745, losses: 92, pending: 831 },
      min_auc: 0.55,
    },
    ...over,
  };
}

let fetchMock: ReturnType<typeof vi.fn>;
let body: EnginesState;

beforeEach(() => {
  resetPolls();
  body = state();
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/engines/reversal/study") {
      return { ok: true, status: 200, json: async () => ({ report: "## Reversal study" }) };
    }
    if (init?.method && init.method !== "GET") {
      return { ok: true, status: 200, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => body };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const posts = () => fetchMock.mock.calls.filter((c) => c[1]?.method === "POST");

/** The Reversal engine's model, switches and ledger now live behind their own
 *  tab, as they did in the NiceGUI panel this restores. */
async function openReversalTab() {
  await userEvent.click(await screen.findByRole("tab", { name: /Reversal engine/ }));
}

describe("what is running", () => {
  it("shows the engines this build has, by the names the operator uses", async () => {
    render(<EnginesPanel />);

    expect(await screen.findByText("Breakout")).toBeInTheDocument();
    // Bounce left the screen on 2026-09-19: its code was deleted on
    // 2026-09-14, so its card carried a Start button that could not work.
    expect(screen.queryByText("Bounce")).not.toBeInTheDocument();
    expect(screen.getByText("Reversal")).toBeInTheDocument();
  });

  it("tells a stopped engine apart from one that is not built", async () => {
    // Both show "not running". Only one of them can be started, and the
    // difference is what the operator needs to know.
    //
    // Bounce was the example here until 2026-09-19, when it left the screen.
    // The STATE it illustrated is still real -- an engine whose service
    // exists but whose instance was never created -- so the example moved
    // rather than the test going with it.
    body = state({
      engines: [
        { id: "breakout", label: "Breakout", running: true, built: true },
        { id: "reversal", label: "Reversal", running: false, built: false },
      ],
    });
    render(<EnginesPanel />);
    await screen.findByText("Breakout");

    expect(screen.getByTestId("engine-reversal")).toHaveAttribute("data-built", "false");
    expect(screen.getByText("Not built on this install")).toBeInTheDocument();
  });

  it("offers Stop for a running engine and Start for a stopped one", async () => {
    render(<EnginesPanel />);
    await screen.findByText("Breakout");

    expect(within("engine-breakout")).toHaveTextContent("Stop");
    expect(within("engine-reversal")).toHaveTextContent("Start");
  });

  it("will not offer to start an engine that is not built, and says why", async () => {
    body = state({
      engines: [
        { id: "breakout", label: "Breakout", running: true, built: true },
        { id: "reversal", label: "Reversal", running: false, built: false },
      ],
    });
    render(<EnginesPanel />);
    await screen.findByText("Breakout");

    const button = within("engine-reversal").querySelector("button")!;
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", expect.stringContaining("not built"));
  });
});

function within(testId: string): HTMLElement {
  return screen.getByTestId(testId);
}

describe("starting and stopping", () => {
  it("starts exactly one engine, by name", async () => {
    render(<EnginesPanel />);
    await screen.findByText("Breakout");

    await userEvent.click(within("engine-reversal").querySelector("button")!);

    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0][0]).toBe("/api/engines/running");
    expect(JSON.parse(posts()[0][1].body)).toEqual({ engine: "reversal", running: true });
  });

  it("stops exactly one engine, by name", async () => {
    render(<EnginesPanel />);
    await screen.findByText("Breakout");

    await userEvent.click(within("engine-breakout").querySelector("button")!);

    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(JSON.parse(posts()[0][1].body)).toEqual({ engine: "breakout", running: false });
  });

  it("shows a refusal from the backend", async () => {
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return {
          ok: false, status: 409, statusText: "",
          json: async () => ({
            error: { kind: "refusal", message: "Nothing to start.", ref: null },
          }),
        };
      }
      return { ok: true, status: 200, json: async () => body };
    });
    render(<EnginesPanel />);
    await screen.findByText("Breakout");

    await userEvent.click(within("engine-reversal").querySelector("button")!);

    expect(await screen.findByRole("alert")).toHaveTextContent("Nothing to start.");
  });
});

describe("the pro-signal model", () => {
  // DELETED 2026-09-19: "says what it was trained on" and "says when it has
  // never been trained". Both asserted on `{trained, samples}` -- a shape
  // `pro_model.status()` has never returned. It returns `ready`, `auc`, `n`,
  // `reason`, `fitted_at`, `corpus` and `min_auc`, so the panel rendered "not
  // trained yet" on every install regardless of the model, and these two
  // tests were what made that look covered. The real payload is tested in
  // ModelSection.test.tsx.

  it("retrains in the background, and says so on the screen", async () => {
    // bugs/030: an inline fit freezes the dashboard, the EA socket reader and
    // the monitor loop together. The operator is told which one this is.
    render(<EnginesPanel />);
    await openReversalTab();

    await userEvent.click(screen.getByRole("button", { name: /Retrain in the background/ }));

    await waitFor(() => {
      expect(posts().some((c) => c[0] === "/api/engines/reversal/fit")).toBe(true);
    });
    expect(screen.getByText(/Doing it inline would freeze/)).toBeInTheDocument();
  });

  it("shows the research study's report", async () => {
    render(<EnginesPanel />);
    await openReversalTab();

    await userEvent.click(screen.getByRole("button", { name: /Run the research study/ }));

    expect(await screen.findByTestId("study-report")).toHaveTextContent("Reversal study");
  });
});


describe("the reversal capabilities", () => {
  it("offers the Asian-session exemption", async () => {
    // Its NiceGUI card was deleted with the reversal panel; the guard in
    // tests/risk/test_htf_bias_gate_asian_exemption.py went red the moment
    // this tab was marked ported without it, which is what it is for.
    render(<EnginesPanel />);
    await openReversalTab();

    expect(await screen.findByTestId("capability-htf_bias_asian_exempt")).toBeInTheDocument();
  });

  it("says the trend gate has to be on first, and that it is not", async () => {
    // "Does NOTHING unless X is also on" was a tooltip. A tooltip is not where
    // you put the reason a setting has no effect.
    render(<EnginesPanel />);
    await openReversalTab();

    const card = await screen.findByTestId("capability-htf_bias_asian_exempt");
    expect(card).toHaveTextContent("Only trade with the trend");
    expect(card).toHaveTextContent("Settings → Risk");
    expect(card).toHaveTextContent("it is currently off");
  });

  it("stops warning once the dependency is on", async () => {
    body = state({
      settings: { htf_bias_asian_exempt: 1, htf_bias_gate_enabled: 1 },
    });
    render(<EnginesPanel />);
    await openReversalTab();

    const card = await screen.findByTestId("capability-htf_bias_asian_exempt");
    expect(card).not.toHaveTextContent("it is currently off");
    expect(screen.getByLabelText(/ignore the trend there/)).toBeChecked();
  });

  it("writes only the switch that changed", async () => {
    render(<EnginesPanel />);
    await openReversalTab();
    await screen.findByTestId("capability-htf_bias_asian_exempt");

    await userEvent.click(screen.getByLabelText(/ignore the trend there/));

    await waitFor(() => {
      const put = fetchMock.mock.calls.find((c) => c[1]?.method === "PUT");
      expect(put).toBeTruthy();
      expect(put![0]).toBe("/api/engines/settings");
      expect(JSON.parse(put![1].body)).toEqual({ htf_bias_asian_exempt: 1 });
    });
  });
});

describe("which machine these controls drive", () => {
  it("says nothing at all when this machine is the one trading", async () => {
    // The ordinary case. A banner on every render is a banner nobody reads.
    render(<EnginesPanel />);
    await screen.findByText("Breakout");

    expect(screen.queryByTestId("control-target")).not.toBeInTheDocument();
  });

  it("says so when the controls will reach the remote node instead", async () => {
    // Without this an operator presses Stop, the local stood-down copy is
    // untouched, the VPS keeps generating, and the screen says stopped.
    body = state({ control_target: "remote" });
    render(<EnginesPanel />);

    const banner = await screen.findByTestId("control-target");
    expect(banner).toHaveTextContent(/act on\s+it/);
    expect(banner).toHaveTextContent(/not on this machine/);
  });

  it("warns that a tunable other than the AI switch cannot travel", async () => {
    // The sync protocol carries exactly one risk setting. Saving any other
    // switch in Remote mode writes a row the trading node never reads.
    body = state({ control_target: "remote" });
    render(<EnginesPanel />);

    expect(await screen.findByTestId("control-target"))
      .toHaveTextContent(/no route between nodes/);
  });

  it("distinguishes centralized generation from plain remote", async () => {
    // The VPS is trading, so the header says REMOTE — and these engines are
    // the live ones, because generation moved here. Reading that as "remote"
    // would send every control to a VPS whose engines stopped analysing.
    body = state({ control_target: "centralized" });
    render(<EnginesPanel />);

    const banner = await screen.findByTestId("control-target");
    expect(banner).toHaveTextContent(/generation has moved here/);
    expect(banner).toHaveTextContent(/act on\s+this machine/);
  });

  it("treats a backend that does not say as local", async () => {
    // An older backend has no such field, and the state this tab has always
    // assumed is the one that renders no banner.
    body = state();
    delete (body as Partial<EnginesState>).control_target;
    render(<EnginesPanel />);
    await screen.findByText("Breakout");

    expect(screen.queryByTestId("control-target")).not.toBeInTheDocument();
  });
});

describe("the two engines have a tab each", () => {
  /**
   * The NiceGUI Signal Generator tab had exactly this: a Breakout tab and a
   * Reversal Engine tab (`frontend/pages/test_panel/__init__.py`). The React
   * port put both engines' detail on one scrolling page, so reading the
   * Breakout splits meant scrolling past the reversal model, its capability
   * switches and its virtual trade ledger. Asked for on 2026-09-20.
   */
  it("opens on Reversal, which is the engine that trades", async () => {
    // Reversed on 2026-09-21. The tab opened on Breakout from 2026-09-20,
    // matching the NiceGUI order; the owner asked for Reversal first
    // ("this is the main one"), so the page now opens on the engine whose
    // model, switches and ledger are the ones actually read each day.
    render(<EnginesPanel />);

    expect(await screen.findByRole("tab", { name: /Reversal engine/ }))
      .toHaveAttribute("aria-selected", "true");
  });

  it("lists Reversal before Breakout", async () => {
    render(<EnginesPanel />);
    await screen.findByRole("tab", { name: /Reversal engine/ });

    const names = screen.getAllByRole("tab").map((t) => t.textContent);
    expect(names).toEqual(["Reversal engine", "Breakout engine"]);
  });

  it("keeps the Breakout detail off the Reversal tab", async () => {
    // The whole point of the split: one engine's numbers at a time.
    render(<EnginesPanel />);
    await screen.findByRole("tab", { name: /Reversal engine/ });

    expect(screen.queryByTestId("bo-edge")).toBeNull();
  });

  it("shows the reversal detail once its tab is chosen", async () => {
    render(<EnginesPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: /Reversal engine/ }));

    expect(await screen.findByTestId("capability-htf_bias_asian_exempt"))
      .toBeInTheDocument();
  });

  it("still starts and stops either engine from above the tabs", async () => {
    // Start/Stop is not per-tab: an operator stopping an engine should not
    // have to find the right tab first.
    render(<EnginesPanel />);
    await openReversalTab();

    expect(screen.getAllByRole("button", { name: /^(Start|Stop)$/ }).length)
      .toBeGreaterThan(1);
  });
});
