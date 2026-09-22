/**
 * The Schedule screen's controls beyond the start/end boxes.
 *
 * The React port carried a grid of times and nothing else. The NiceGUI screen
 * it replaced also had the Trading Markets toggles, a per-window profit target
 * and a per-source Override for every channel and both engines — three gates
 * that decide whether an automated order is placed at all, none of which were
 * reachable from the dashboard.
 *
 * Every payload here is the shape `/api/schedule/state` really returns.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { ScheduleSection } from "../internal/ScheduleSection";

function block(over: Record<string, unknown> = {}) {
  return {
    start: "08:00", end: "12:00", enabled: true, target: 0,
    reversal_engine: true, breakout_engine: true,
    reversal_engine_override: "", breakout_engine_override: "",
    telegram_channels: {}, telegram_default_enabled: true,
    ...over,
  };
}

// `onSetSchedule` is typed `Record<string, unknown>`, so a captured payload
// reads back as `unknown` per key. Before vitest 4 an untyped mock made it
// `any` and the assertions below walked straight in; this names the shape the
// component actually sends instead of restoring that looseness.
type SentBlock = {
  start: string;
  end: string;
  enabled: boolean;
  target: number;
  reversal_engine: boolean;
  breakout_engine: boolean;
  reversal_engine_override: string;
  breakout_engine_override: string;
  telegram_channels: Record<
    string,
    { enabled: boolean; strategy_override: string }
  >;
  telegram_default_enabled: boolean;
};

function lastSchedule(): Record<string, SentBlock[]> {
  return setSchedule.mock.calls.at(-1)![0] as Record<string, SentBlock[]>;
}

function state(over: Record<string, unknown> = {}) {
  return {
    schedule: { monday: [block()], tuesday: [block({ enabled: false })] },
    enabled: true,
    daily_target: 250,
    daily_state: { reached: false, overridden: false, pnl: 40, target: 250 },
    clock: { label: "UTC+01:00", configured: 60, following_machine: false,
             now: "2026-09-20T09:15:00" },
    markets: { asia: true, london: true, new_york: false,
               session: "london", allowed_now: true },
    override_options: [
      { value: "", label: "— No Override —" },
      { value: "auto", label: "Auto (AI-managed)" },
      { value: "template:Grid-A", label: "Template: Grid-A" },
    ],
    channels: ["GoldSignals", "GD2"],
    ...over,
  };
}

// Typed to the prop each one stands in for, not to bare `vi.fn`: since
// vitest 4 an untyped mock is `Mock<Procedure | Constructable>`, which no
// longer narrows to a specific call signature.
type ScheduleProps = Parameters<typeof ScheduleSection>[0];
let props: ScheduleProps;
let setSchedule: Mock<NonNullable<ScheduleProps["onSetSchedule"]>>;
let setMarket: Mock<NonNullable<ScheduleProps["onSetMarket"]>>;
let setClockOffset: Mock<NonNullable<ScheduleProps["onSetClockOffset"]>>;

beforeEach(() => {
  setSchedule = vi.fn();
  setMarket = vi.fn();
  setClockOffset = vi.fn();
  props = {
    state: state(),
    onSetEnabled: vi.fn(),
    onSetSchedule: setSchedule,
    onSetTarget: vi.fn(),
    onResumeToday: vi.fn(),
    onSetMarket: setMarket,
    onSetClockOffset: setClockOffset,
  };
});
afterEach(() => vi.restoreAllMocks());

describe("the trading clock", () => {
  it("shows which clock the windows are measured in", () => {
    // The summary line, not the dropdown: the label appears in both, and a
    // bare text match finds the option list too.
    render(<ScheduleSection {...props} />);

    expect(screen.getByText(/UTC\+01:00, set here/)).toBeInTheDocument();
  });

  it("says when it is simply following this machine", () => {
    props.state = state({
      clock: { label: "UTC+01:00", configured: null, following_machine: true,
               now: "2026-09-20T09:15:00" },
    });
    render(<ScheduleSection {...props} />);

    expect(screen.getByText(/following this machine's own clock/))
      .toBeInTheDocument();
  });

  it("offers the quarter-hour zones real places use", () => {
    render(<ScheduleSection {...props} />);

    expect(screen.getByRole("option", { name: "UTC+05:45" })).toBeInTheDocument();
  });

  it("can be put back on the machine's own clock", async () => {
    // The state a single-machine install wants, and the one the endpoint
    // could not express until its `minutes` field accepted null.
    render(<ScheduleSection {...props} />);

    await userEvent.selectOptions(
      screen.getByLabelText("Trading clock"), "machine");

    expect(setClockOffset).toHaveBeenCalledWith(null);
  });

  it("sends UTC as zero, not as the machine clock", async () => {
    render(<ScheduleSection {...props} />);

    await userEvent.selectOptions(screen.getByLabelText("Trading clock"), "0");

    expect(setClockOffset).toHaveBeenCalledWith(0);
  });
});

describe("the trading markets", () => {
  it("shows which session is live", () => {
    render(<ScheduleSection {...props} />);

    expect(screen.getByTestId("current-session")).toHaveTextContent("London");
  });

  it("shows each market's state", () => {
    render(<ScheduleSection {...props} />);

    expect(screen.getByRole("button", { name: /Asia/ }))
      .toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /New York/ }))
      .toHaveAttribute("aria-pressed", "false");
  });

  it("toggles a market to the opposite of what it is", async () => {
    render(<ScheduleSection {...props} />);

    await userEvent.click(screen.getByRole("button", { name: /London/ }));

    expect(setMarket).toHaveBeenCalledWith("london", false);
  });

  it("warns when no session is active at all", () => {
    props.state = state({
      markets: { asia: false, london: false, new_york: false,
                 session: "london", allowed_now: false },
    });
    render(<ScheduleSection {...props} />);

    expect(screen.getByText(/every automated entry is blocked/))
      .toBeInTheDocument();
  });
});

describe("a window's profit target", () => {
  it("is editable", async () => {
    render(<ScheduleSection {...props} />);
    const input = screen.getByLabelText("monday window 1 target");

    await userEvent.clear(input);
    await userEvent.type(input, "75");
    await userEvent.tab();

    await waitFor(() => expect(setSchedule).toHaveBeenCalled());
    const sent = lastSchedule();
    expect(sent.monday[0].target).toBe(75);
  });
});

describe("a window's channels panel", () => {
  it("is collapsed until asked for, and counts what is enabled", () => {
    render(<ScheduleSection {...props} />);

    // 2 channels + 2 engines, all on.
    expect(screen.getAllByText(/Channels \(4\/4\)/).length).toBeGreaterThan(0);
    expect(screen.queryByLabelText(/GoldSignals in monday window 1/))
      .not.toBeInTheDocument();
  });

  it("lists every channel and both engines", async () => {
    render(<ScheduleSection {...props} />);

    await userEvent.click(screen.getAllByText(/Channels \(4\/4\)/)[0]);

    expect(screen.getByLabelText("GoldSignals in monday window 1")).toBeChecked();
    expect(screen.getByLabelText("GD2 in monday window 1")).toBeChecked();
    expect(screen.getByLabelText("Reversal Engine in monday window 1")).toBeChecked();
    expect(screen.getByLabelText("Breakout Engine in monday window 1")).toBeChecked();
  });

  it("blocks one channel without touching the others", async () => {
    render(<ScheduleSection {...props} />);
    await userEvent.click(screen.getAllByText(/Channels \(4\/4\)/)[0]);

    await userEvent.click(screen.getByLabelText("GoldSignals in monday window 1"));

    const sent = lastSchedule();
    expect(sent.monday[0].telegram_channels.GoldSignals.enabled).toBe(false);
    expect(sent.monday[0].breakout_engine).toBe(true);
  });

  it("forces one channel onto a template for this window only", async () => {
    render(<ScheduleSection {...props} />);
    await userEvent.click(screen.getAllByText(/Channels \(4\/4\)/)[0]);

    await userEvent.selectOptions(
      screen.getByLabelText("GoldSignals override in monday window 1"),
      "template:Grid-A");

    const sent = lastSchedule();
    expect(sent.monday[0].telegram_channels.GoldSignals.strategy_override)
      .toBe("template:Grid-A");
  });

  it("gives each engine its own override", async () => {
    // The whole reason engines have separate override fields: Reversal and
    // Breakout can run different strategies inside the same window.
    render(<ScheduleSection {...props} />);
    await userEvent.click(screen.getAllByText(/Channels \(4\/4\)/)[0]);

    await userEvent.selectOptions(
      screen.getByLabelText("Reversal Engine override in monday window 1"),
      "auto");

    const sent = lastSchedule();
    expect(sent.monday[0].reversal_engine_override).toBe("auto");
    expect(sent.monday[0].breakout_engine_override).toBe("");
  });

  it("counts a channel turned off", async () => {
    props.state = state({
      schedule: {
        monday: [block({
          telegram_channels: { GoldSignals: { enabled: false, strategy_override: "" } },
        })],
        tuesday: [block()],
      },
    });
    render(<ScheduleSection {...props} />);

    expect(screen.getAllByText(/Channels \(3\/4\)/).length).toBeGreaterThan(0);
  });
});

describe("copy Monday to all days", () => {
  it("copies Monday's windows over every other day", async () => {
    render(<ScheduleSection {...props} />);

    await userEvent.click(screen.getByText(/Copy Monday to all days/));

    const sent = lastSchedule();
    expect(sent.tuesday[0].enabled).toBe(true);
    expect(sent.tuesday[0].start).toBe("08:00");
  });

  it("leaves Monday itself alone", async () => {
    render(<ScheduleSection {...props} />);

    await userEvent.click(screen.getByText(/Copy Monday to all days/));

    const sent = lastSchedule();
    expect(sent.monday[0].start).toBe("08:00");
  });
});
