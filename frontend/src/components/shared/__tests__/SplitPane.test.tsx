import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SplitPane } from "../SplitPane";

/** This repository's jsdom provides no `localStorage` at all. */
function fakeStorage(over: Partial<Storage> = {}): Storage {
  const held = new Map<string, string>();
  return {
    getItem: (k: string) => held.get(k) ?? null,
    setItem: (k: string, v: string) => { held.set(k, v); },
    removeItem: (k: string) => { held.delete(k); },
    clear: () => held.clear(),
    key: () => null,
    get length() { return held.size; },
    ...over,
  } as Storage;
}

/**
 * "may be easier to make the tables adjustable on the screen" — 2026-09-21.
 *
 * The Chart tab put a fixed 20rem beside the candles, and the open-positions
 * table has nine columns. How much of each is wanted changes with the screen
 * and with what is being looked at, so it is the operator's to set.
 */
beforeEach(() => vi.stubGlobal("localStorage", fakeStorage()));
afterEach(() => vi.unstubAllGlobals());

const pane = (over: Record<string, unknown> = {}) => (
  <SplitPane
    storageKey="test-split"
    defaultRightPct={30}
    left={<p>the chart</p>}
    right={<p>the table</p>}
    {...over}
  />
);

function widthOf(): string {
  return (screen.getByText("the table").parentElement as HTMLElement).style.flexBasis;
}

describe("SplitPane", () => {
  it("shows both panes", () => {
    render(pane());

    expect(screen.getByText("the chart")).toBeInTheDocument();
    expect(screen.getByText("the table")).toBeInTheDocument();
  });

  it("opens at the default split", () => {
    render(pane());

    expect(widthOf()).toBe("30%");
  });

  it("gives the right-hand pane more room with the keyboard", async () => {
    render(pane());
    screen.getByRole("separator").focus();

    await userEvent.keyboard("{ArrowLeft}");

    expect(widthOf()).toBe("32%");
  });

  it("gives it less with the other arrow", async () => {
    render(pane());
    screen.getByRole("separator").focus();

    await userEvent.keyboard("{ArrowRight}");

    expect(widthOf()).toBe("28%");
  });

  it("will not let a pane be dragged away entirely", async () => {
    // A split of 0 leaves a control the operator cannot get back without
    // clearing site data.
    render(pane({ minRightPct: 20 }));
    screen.getByRole("separator").focus();

    await userEvent.keyboard("{ArrowRight}".repeat(20));

    expect(widthOf()).toBe("20%");
  });

  it("goes back to the default on a double click", async () => {
    render(pane());
    screen.getByRole("separator").focus();
    await userEvent.keyboard("{ArrowLeft}");

    await userEvent.dblClick(screen.getByRole("separator"));

    expect(widthOf()).toBe("30%");
  });

  it("remembers the split for next time", async () => {
    const { unmount } = render(pane());
    screen.getByRole("separator").focus();
    await userEvent.keyboard("{ArrowLeft}");
    unmount();

    render(pane());

    expect(widthOf()).toBe("32%");
  });

  it("falls back to the default when the browser will not store it", () => {
    // Private windows and blocked site data both throw here. A remembered
    // width is a convenience; losing it must not take the page with it.
    vi.stubGlobal("localStorage", fakeStorage({
      getItem: () => { throw new Error("blocked"); },
    }));

    render(pane());

    expect(widthOf()).toBe("30%");
  });

  it("ignores a remembered value that is not a number", () => {
    localStorage.setItem("test-split", "not a number");

    render(pane());

    expect(widthOf()).toBe("30%");
  });

  it("tracks the pointer while the divider is dragged", () => {
    render(pane());
    const host = screen.getByRole("separator").parentElement as HTMLElement;
    host.getBoundingClientRect = () =>
      ({ left: 0, right: 1000, width: 1000, top: 0, bottom: 500, height: 500 }) as DOMRect;

    fireEvent.mouseDown(screen.getByRole("separator"));
    act(() => { fireEvent.mouseMove(window, { clientX: 600 }); });

    expect(widthOf()).toBe("40%");
  });

  it("stops tracking once the pointer is released", () => {
    render(pane());
    const host = screen.getByRole("separator").parentElement as HTMLElement;
    host.getBoundingClientRect = () =>
      ({ left: 0, right: 1000, width: 1000, top: 0, bottom: 500, height: 500 }) as DOMRect;
    fireEvent.mouseDown(screen.getByRole("separator"));
    act(() => { fireEvent.mouseMove(window, { clientX: 600 }); });

    act(() => { fireEvent.mouseUp(window); });
    act(() => { fireEvent.mouseMove(window, { clientX: 900 }); });

    expect(widthOf()).toBe("40%");
  });

  it("announces where the divider is to assistive tech", () => {
    render(pane());

    expect(screen.getByRole("separator")).toHaveAttribute("aria-valuenow", "30");
  });
});

/**
 * The chart came back 30 pixels tall — a time axis and no candles — on
 * 2026-09-22. Nothing was wrong with the data or with lightweight-charts.
 *
 * `height: 100%` resolves against a containing block whose own height is
 * definite. Each slot here was a plain `div`, so the panel inside it was a
 * block box at content height, and every percentage height below it collapsed
 * to the tallest thing that could size itself: the chart's time scale. A
 * full-height child needs its slot to be a flex column so the panel it holds
 * is a flex item with a height flex layout has already resolved.
 *
 * jsdom does no layout, so what is pinned here is the contract that makes the
 * layout possible, not the pixels it produced.
 */
describe("a pane that wants the full height", () => {
  it("lays each slot out as a flex column", () => {
    render(pane());

    for (const text of ["the chart", "the table"]) {
      const slot = screen.getByText(text).parentElement as HTMLElement;
      expect(slot.className).toContain("flex-col");
      expect(slot.className).toContain("min-h-0");
    }
  });
});
