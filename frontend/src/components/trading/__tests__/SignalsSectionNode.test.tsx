/**
 * The Signals table on a Mac that trades through the VPS (owner, 2026-10-05:
 * "this should show all of the signals from the active node").
 *
 * The rows now come from the VPS, tagged `node: "remote"`. Two things follow:
 *
 *  * Edit writes to THIS node's database, where a VPS signal does not exist,
 *    so it is disabled on those rows and says where to edit instead;
 *  * when the VPS cannot answer, the route is a 503 with the reason. The
 *    table says that, rather than "No signals yet", which would read as a
 *    quiet day.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SignalsSection } from "../internal/SignalsSection";

const row = (over: Record<string, unknown>) => ({
  signal_id: "s1", direction: "BUY", entry: 4000, entry_high: 4002, sl: 3990,
  tp1: 4010, status: "pending", created_at: 1_759_600_000, source: "Chan", ...over,
});

describe("rows from the VPS", () => {
  it("cannot be edited here, and says why", () => {
    render(<SignalsSection signals={[row({ node: "remote" })]} onChanged={() => {}} />);

    const edit = screen.getByRole("button", { name: "Edit" });
    expect(edit).toBeDisabled();
    expect(edit.getAttribute("title")).toMatch(/VPS/);
  });

  it("says whose signals these are", () => {
    render(<SignalsSection signals={[row({ node: "remote" })]} onChanged={() => {}} />);

    expect(screen.getByText(/from the VPS/i)).toBeInTheDocument();
  });

  it("leaves this node's own rows editable", () => {
    // Negative control: a node that trades itself is unchanged.
    render(<SignalsSection signals={[row({})]} onChanged={() => {}} />);

    expect(screen.getByRole("button", { name: "Edit" })).toBeEnabled();
    expect(screen.queryByText(/from the VPS/i)).not.toBeInTheDocument();
  });
});

describe("a trading node that cannot answer", () => {
  it("says so instead of 'No signals yet'", () => {
    render(
      <SignalsSection
        signals={[]}
        error="The trading node could not be reached (not connected to VPS)."
        onChanged={() => {}}
      />,
    );

    expect(screen.getByText(/could not be reached/)).toBeInTheDocument();
    expect(screen.queryByText("No signals yet")).not.toBeInTheDocument();
  });
});
