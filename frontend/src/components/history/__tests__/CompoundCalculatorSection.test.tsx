import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CompoundCalculatorSection } from "../internal/CompoundCalculatorSection";
import { formatMoney } from "@/components/shared/format";

/**
 * The Compound Calculator tab: a daily goal, compounded.
 *
 * Every figure is arithmetic on what is typed. The tests pin the numbers the
 * owner reads -- the final balance, the weekly and monthly rows -- and the
 * two things that stop a projection being read as a forecast: the line at
 * half the goal, and the sentence saying what the numbers assume.
 */
/** This repository's jsdom provides no `localStorage` at all. */
function fakeStorage(): Storage {
  const held = new Map<string, string>();
  return {
    getItem: (k: string) => held.get(k) ?? null,
    setItem: (k: string, v: string) => { held.set(k, v); },
    removeItem: (k: string) => { held.delete(k); },
    clear: () => held.clear(),
    key: () => null,
    get length() { return held.size; },
  } as Storage;
}

beforeEach(() => vi.stubGlobal("localStorage", fakeStorage()));
afterEach(() => vi.unstubAllGlobals());

function setField(label: string, value: string) {
  const box = screen.getByLabelText(label);
  fireEvent.change(box, { target: { value } });
}

describe("the inputs", () => {
  it("starts from the balance it is given", () => {
    render(<CompoundCalculatorSection balance={2500} />);

    expect(screen.getByLabelText("Starting capital")).toHaveValue(2500);
  });

  it("starts from $10,000 when there is no balance to use", () => {
    render(<CompoundCalculatorSection balance={null} />);

    expect(screen.getByLabelText("Starting capital")).toHaveValue(10000);
  });

  it("puts the account balance back after it has been changed", async () => {
    render(<CompoundCalculatorSection balance={2500} />);
    setField("Starting capital", "900");

    await userEvent.click(screen.getByRole("button", { name: "Use account balance" }));

    expect(screen.getByLabelText("Starting capital")).toHaveValue(2500);
  });

  it("remembers the goal between visits, but not the capital", () => {
    const first = render(<CompoundCalculatorSection balance={1000} />);
    setField("Daily goal (%)", "0.5");
    setField("Starting capital", "777");
    first.unmount();

    render(<CompoundCalculatorSection balance={1000} />);

    expect(screen.getByLabelText("Daily goal (%)")).toHaveValue(0.5);
    expect(screen.getByLabelText("Starting capital")).toHaveValue(1000);
  });
});

describe("the headline figures", () => {
  it("compounds the goal over a year of five-day weeks", () => {
    render(<CompoundCalculatorSection balance={1000} />);

    expect(screen.getByTestId("cc-final")).toHaveTextContent(formatMoney(1000 * 1.01 ** 260));
    expect(screen.getByTestId("cc-profit")).toHaveTextContent(formatMoney(1000 * 1.01 ** 260 - 1000));
  });

  it("follows the goal when it changes", () => {
    render(<CompoundCalculatorSection balance={1000} />);

    setField("Daily goal (%)", "0.5");

    expect(screen.getByTestId("cc-final")).toHaveTextContent(formatMoney(1000 * 1.005 ** 260));
  });

  it("follows the days per week", () => {
    render(<CompoundCalculatorSection balance={1000} />);

    setField("Trading days per week", "3");

    expect(screen.getByTestId("cc-final")).toHaveTextContent(formatMoney(1000 * 1.01 ** 156));
  });

  it("gives the first day's target in dollars", () => {
    render(<CompoundCalculatorSection balance={2500} />);

    expect(screen.getByTestId("cc-day-one")).toHaveTextContent("$25.00");
  });

  it("says what the same months come to at half the goal", () => {
    // A goal hit every single day is the best case. Half of it is a far more
    // ordinary year, and seeing both is what keeps the first honest.
    render(<CompoundCalculatorSection balance={1000} />);

    expect(screen.getByTestId("cc-half")).toHaveTextContent(formatMoney(1000 * 1.005 ** 260));
  });

  it("says the numbers assume the goal is hit every trading day", () => {
    render(<CompoundCalculatorSection balance={1000} />);

    expect(screen.getByText(/every trading day, with no losing days/)).toBeInTheDocument();
  });

  it("shows a message rather than numbers for a blank capital", () => {
    render(<CompoundCalculatorSection balance={1000} />);

    setField("Starting capital", "");

    expect(screen.queryByTestId("cc-final")).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent(/starting capital/i);
  });
});

describe("the breakdown", () => {
  it("lists a row per month by default", () => {
    render(<CompoundCalculatorSection balance={1000} />);

    const table = screen.getByRole("table");
    // One header row, twelve months.
    expect(within(table).getAllByRole("row")).toHaveLength(13);
    expect(within(table).getByText("Month 1")).toBeInTheDocument();
  });

  it("lists a row per week when asked", async () => {
    render(<CompoundCalculatorSection balance={1000} />);

    await userEvent.click(screen.getByRole("button", { name: "Weekly" }));

    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(53);
    const first = within(table).getAllByRole("row")[1];
    expect(first).toHaveTextContent("Week 1");
    expect(first).toHaveTextContent(formatMoney(1000 * 1.01 ** 5 - 1000));
    expect(first).toHaveTextContent("5.10%");
  });

  it("shows the money paid in only when there is some", () => {
    render(<CompoundCalculatorSection balance={1000} />);
    expect(within(screen.getByRole("table")).queryByText("Paid in")).toBeNull();

    setField("Monthly deposit", "200");

    expect(within(screen.getByRole("table")).getByText("Paid in")).toBeInTheDocument();
    expect(screen.getByTestId("cc-deposited")).toHaveTextContent("$2,400.00");
  });

  it("shows the profit taken out only when some is", () => {
    render(<CompoundCalculatorSection balance={1000} />);
    expect(within(screen.getByRole("table")).queryByText("Taken out")).toBeNull();

    setField("Profit reinvested (%)", "0");

    expect(within(screen.getByRole("table")).getByText("Taken out")).toBeInTheDocument();
    expect(screen.getByTestId("cc-final")).toHaveTextContent("$1,000.00");
    expect(screen.getByTestId("cc-withdrawn")).toHaveTextContent("$2,600.00");
  });
});

describe("the chart", () => {
  it("draws the goal, half the goal and the money put in", () => {
    render(<CompoundCalculatorSection balance={1000} />);

    expect(screen.getByTestId("cc-line-goal")).toBeInTheDocument();
    expect(screen.getByTestId("cc-line-half")).toBeInTheDocument();
    expect(screen.getByTestId("cc-line-paid")).toBeInTheDocument();
  });

  it("shows profit per month and per week side by side, not one or the other", () => {
    // Owner, 2026-09-29: both at once, each half the width.
    render(<CompoundCalculatorSection balance={1000} />);

    const month = screen.getByRole("img", { name: "Projected profit per month" });
    const week = screen.getByRole("img", { name: "Projected profit per week" });
    expect(month.querySelectorAll("rect")).toHaveLength(12);
    expect(week.querySelectorAll("rect")).toHaveLength(52);
    expect(month.closest("[data-testid=cc-bars]"))
      .toBe(week.closest("[data-testid=cc-bars]"));
  });

  it("switches to a log scale, so a hockey stick can be read", async () => {
    render(<CompoundCalculatorSection balance={1000} />);
    const linear = screen.getByTestId("cc-line-goal").getAttribute("d");

    await userEvent.click(screen.getByRole("button", { name: "Log scale" }));

    expect(screen.getByRole("button", { name: "Log scale" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("cc-line-goal").getAttribute("d")).not.toBe(linear);
  });
});
