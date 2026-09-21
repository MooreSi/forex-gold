import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { LabelWithHelp, Tooltip } from "../Tooltip";

describe("Tooltip", () => {
  it("says nothing until the control is hovered", () => {
    render(<Tooltip label="Risk per entry"><button>Risk</button></Tooltip>);

    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("explains the control on hover", async () => {
    render(<Tooltip label="Risk per entry"><button>Risk</button></Tooltip>);

    await userEvent.hover(screen.getByRole("button", { name: "Risk" }));

    expect(screen.getByRole("tooltip")).toHaveTextContent("Risk per entry");
  });

  it("takes it away again when the pointer leaves", async () => {
    render(<Tooltip label="Risk per entry"><button>Risk</button></Tooltip>);
    const button = screen.getByRole("button", { name: "Risk" });

    await userEvent.hover(button);
    await userEvent.unhover(button);

    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("explains it on keyboard focus too", async () => {
    // A tooltip only a pointer can open is one that does not exist for
    // anybody working from the keyboard.
    render(<Tooltip label="Risk per entry"><button>Risk</button></Tooltip>);

    await userEvent.tab();

    expect(screen.getByRole("tooltip")).toHaveTextContent("Risk per entry");
  });

  it("renders the control untouched when there is nothing to say", () => {
    render(<Tooltip label={undefined}><button>Risk</button></Tooltip>);

    expect(screen.getByRole("button", { name: "Risk" })).toBeInTheDocument();
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("wraps the control in nothing that takes up space", async () => {
    // `display: contents`. This goes around inputs and table cells across
    // every panel; a wrapper with a box would re-lay-out all of them.
    const { container } = render(
      <Tooltip label="hello"><input aria-label="Lots" /></Tooltip>,
    );

    expect((container.firstElementChild as HTMLElement).className).toContain("contents");
  });

  it("points the bubble at the control, not at the corner of the page", async () => {
    // The wrapper has no box of its own, so measuring IT puts every bubble at
    // 0,0 — on top of the FOREX Trader brand in the header.
    const { container } = render(
      <Tooltip label="Risk per entry"><button>Risk</button></Tooltip>,
    );
    const button = screen.getByRole("button", { name: "Risk" });
    button.getBoundingClientRect = () =>
      ({ left: 400, top: 300, width: 80, height: 20, bottom: 320, right: 480 }) as DOMRect;
    (container.firstElementChild as HTMLElement).getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 0, height: 0, bottom: 0, right: 0 }) as DOMRect;

    await userEvent.hover(button);

    expect(screen.getByRole("tooltip")).toHaveStyle({ left: "440px" });
  });

  it("describes the control to assistive tech while it is open", async () => {
    render(<Tooltip label="Risk per entry"><button>Risk</button></Tooltip>);

    await userEvent.hover(screen.getByRole("button", { name: "Risk" }));

    expect(screen.getByRole("tooltip").id).not.toBe("");
  });
});

describe("LabelWithHelp", () => {
  it("shows the label with no mark when there is no help", () => {
    render(<LabelWithHelp label="Lots" />);

    expect(screen.getByText("Lots")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("puts the explanation behind a mark anyone can reach", async () => {
    render(<LabelWithHelp label="Lots" help="How much each entry risks." />);

    await userEvent.hover(screen.getByRole("button", { name: "What is Lots?" }));

    expect(screen.getByRole("tooltip")).toHaveTextContent("How much each entry risks.");
  });
});
