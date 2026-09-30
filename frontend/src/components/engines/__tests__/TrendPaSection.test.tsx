import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TrendPaSection } from "../internal/TrendPaSection";
import { resetPolls } from "@/hooks/usePoll";

/**
 * The Trend PA engine's panel (docs/todo/012). The body is the shape
 * `services/trend_pa/panel_data.local_report` returns -- `win_rate` is a
 * FRACTION there, which is the kind of unit the Breakout panel once rendered
 * as "1895.3%".
 */
const summary = (over: Record<string, unknown> = {}) => ({
  n: 533, wins: 188, losses: 330, timeouts: 15, win_rate: 0.353,
  breakeven_win_rate: 1 / 3, total_r: 28.5, avg_r: 0.054, expectancy_r: 0.054,
  profit_factor: 1.08, max_drawdown_r: 28.1, balance: 1260, start_balance: 1000,
  max_drawdown_pct: 25.9,
  by_session: [{ key: "overlap", n: 131, wins: 51, win_rate: 0.389, total_r: 21, avg_r: 0.16 }],
  by_pattern: [{ key: "pin", n: 308, wins: 115, win_rate: 0.373, total_r: 30.5, avg_r: 0.099 }],
  by_direction: [{ key: "BUY", n: 337, wins: 115, win_rate: 0.341, total_r: 9.8, avg_r: 0.029 }],
  curve: [{ ts: 1, balance: 1010, r: 1 }, { ts: 2, balance: 1000, r: 0 }],
  ...over,
});

let body: Record<string, unknown>;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  resetPolls();
  body = {
    where: "local", running: true, status: "no clear H4 trend",
    live: summary({ n: 0, wins: 0, losses: 0, timeouts: 0, win_rate: null, total_r: 0,
                    avg_r: null, profit_factor: null, balance: 1000, max_drawdown_r: 0,
                    by_session: [], by_pattern: [], by_direction: [], curve: [] }),
    backtest: summary(),
    backtest_at: 1790670000, backtest_running: false,
    open: [], recent: [], log: [{ ts: 1790670000, reason: "outside London/New York" }],
    ml: { armed: false, n: 533, auc: 0.51, why: "holdout AUC 0.510 below 0.55",
          min_samples: 60, min_auc: 0.55 },
    rules: { rr: 2, session_start_utc: 8, session_end_utc: 21 },
  };
  fetchMock = vi.fn(async (_url: string, init?: RequestInit) => ({
    ok: true, status: 200,
    json: async () => (init?.method === "POST" ? { started: true, where: "local" } : body),
  }));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPolls();
  vi.unstubAllGlobals();
});

const posts = () => fetchMock.mock.calls.filter((c) => c[1]?.method === "POST");

describe("the evidence", () => {
  it("puts the backtest beside the live record, with the counts", async () => {
    render(<TrendPaSection settings={{}} onSaveSetting={vi.fn()} />);
    const bt = await screen.findByTestId("tpa-backtest");
    expect(within(bt).getByTestId("tpa-n")).toHaveTextContent("533");
    expect(within(bt).getByTestId("tpa-win-rate")).toHaveTextContent("35.3%");
    expect(within(bt).getByTestId("tpa-win-rate")).toHaveTextContent("33.3%");
    expect(within(bt).getByTestId("tpa-avg-r")).toHaveTextContent("+0.054R");
    expect(within(bt).getByTestId("tpa-pf")).toHaveTextContent("1.08");
    const live = screen.getByTestId("tpa-live");
    expect(within(live).getByTestId("tpa-n")).toHaveTextContent("0");
    expect(within(live).getByTestId("tpa-pf")).toHaveTextContent("not yet");
  });

  it("says which node the numbers came from", async () => {
    body.where = "remote";
    render(<TrendPaSection settings={{}} onSaveSetting={vi.fn()} />);
    expect(await screen.findByTestId("tpa-where")).toHaveTextContent(/VPS/);
  });

  it("says why the model has no say yet", async () => {
    render(<TrendPaSection settings={{}} onSaveSetting={vi.fn()} />);
    expect(await screen.findByTestId("tpa-ml")).toHaveTextContent("holdout AUC 0.510 below 0.55");
    expect(screen.getByTestId("tpa-ml")).toHaveTextContent(/not armed/i);
  });

  it("shows the splits", async () => {
    render(<TrendPaSection settings={{}} onSaveSetting={vi.fn()} />);
    expect(await screen.findByTestId("tpa-split-session")).toHaveTextContent("overlap");
  });

  it("shows why it did not trade", async () => {
    render(<TrendPaSection settings={{}} onSaveSetting={vi.fn()} />);
    expect(await screen.findByTestId("tpa-log")).toHaveTextContent("outside London/New York");
  });
});

describe("the liveness line", () => {
  const now = () => Date.now() / 1000;
  const alive = (over: Record<string, unknown> = {}) => {
    body.generating_here = true;
    body.last_cycle_at = now() - 20;
    body.last_evaluated_at = now() - 20;
    body.generated_at = now() - 2;
    Object.assign(body, over);
  };

  it("says where it is analysing and how long ago it last looked", async () => {
    body.where = "remote";
    alive({ last_evaluated_at: now() - 150 });
    render(<TrendPaSection settings={{}} onSaveSetting={vi.fn()} />);
    const line = await screen.findByTestId("tpa-alive");
    expect(line).toHaveTextContent(/analysing on the vps/i);
    expect(line).toHaveTextContent(/last checked 2 min ago/i);
  });

  it("says this machine when the report is local", async () => {
    alive();
    render(<TrendPaSection settings={{}} onSaveSetting={vi.fn()} />);
    expect(await screen.findByTestId("tpa-alive")).toHaveTextContent(/analysing on this machine/i);
  });

  it("calls a long silence a stall, not a quiet market", async () => {
    alive({ last_evaluated_at: now() - 900 });
    render(<TrendPaSection settings={{}} onSaveSetting={vi.fn()} />);
    expect(await screen.findByTestId("tpa-alive")).toHaveTextContent(/not analysing.*15 min ago/i);
  });

  it("says when this node is running but the other one does the analysing", async () => {
    alive({ generating_here: false, last_evaluated_at: null });
    render(<TrendPaSection settings={{}} onSaveSetting={vi.fn()} />);
    expect(await screen.findByTestId("tpa-alive")).toHaveTextContent(/not analysing on this machine/i);
  });

  it("says when the engine is stopped", async () => {
    alive({ running: false });
    render(<TrendPaSection settings={{}} onSaveSetting={vi.fn()} />);
    expect(await screen.findByTestId("tpa-alive")).toHaveTextContent(/stopped/i);
  });

  it("says when the VPS report itself has gone stale", async () => {
    body.where = "remote";
    alive({ generated_at: now() - 600, last_evaluated_at: now() - 610 });
    render(<TrendPaSection settings={{}} onSaveSetting={vi.fn()} />);
    expect(await screen.findByTestId("tpa-alive")).toHaveTextContent(/report is 10 min old/i);
  });

  it("an older node that reports nothing of this says nothing rather than guessing", async () => {
    render(<TrendPaSection settings={{}} onSaveSetting={vi.fn()} />);
    await screen.findByTestId("tpa-where");
    expect(screen.queryByTestId("tpa-alive")).toBeNull();
  });
});

describe("the backtest button", () => {
  it("starts a replay with a POST", async () => {
    render(<TrendPaSection settings={{}} onSaveSetting={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: /run the backtest/i }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(String(posts()[0][0])).toContain("/api/engines/trend-pa/backtest");
  });

  it("cannot be pressed while one is running, and says so", async () => {
    body.backtest_running = true;
    render(<TrendPaSection settings={{}} onSaveSetting={vi.fn()} />);
    const btn = await screen.findByRole("button", { name: /backtest running/i });
    expect(btn).toBeDisabled();
  });
});

describe("the live switch", () => {
  it("turning it on asks first, then saves 1", async () => {
    const save = vi.fn();
    render(<TrendPaSection settings={{ tpa_live_execution: 0 }} onSaveSetting={save} />);
    await userEvent.click(await screen.findByLabelText(/place real orders/i));
    expect(save).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /yes, place real orders/i }));
    expect(save).toHaveBeenCalledWith("tpa_live_execution", 1);
  });

  it("backing out of the question saves nothing", async () => {
    const save = vi.fn();
    render(<TrendPaSection settings={{ tpa_live_execution: 0 }} onSaveSetting={save} />);
    await userEvent.click(await screen.findByLabelText(/place real orders/i));
    await userEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(save).not.toHaveBeenCalled();
  });

  it("turning it off needs no question", async () => {
    const save = vi.fn();
    render(<TrendPaSection settings={{ tpa_live_execution: 1 }} onSaveSetting={save} />);
    await userEvent.click(await screen.findByLabelText(/place real orders/i));
    expect(save).toHaveBeenCalledWith("tpa_live_execution", 0);
  });
});

it("a failed read says so instead of drawing zeros", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) })));
  render(<TrendPaSection settings={{}} onSaveSetting={vi.fn()} />);
  expect(await screen.findByText(/could not load the trend pa panel/i)).toBeInTheDocument();
});
