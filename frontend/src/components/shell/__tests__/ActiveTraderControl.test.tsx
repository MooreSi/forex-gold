import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ActiveTraderControl } from "../ActiveTraderControl";

/**
 * The only control in the shell that changes trading behaviour.
 *
 * Both paired nodes point at the same MT5 account, so the questions here are
 * not cosmetic: does a click go straight through, does the operator learn what
 * the backend actually did, and does a refusal reach them in the backend's own
 * words rather than as a shrug.
 *
 * The handshake itself lives in the backend and is tested there. Nothing in
 * this file can reach a node, a broker or a socket: `fetch` is a recorder.
 */
let fetchMock: ReturnType<typeof vi.fn>;
let response: { status: number; body: unknown };

beforeEach(() => {
  response = {
    status: 200,
    body: { active_trader: "remote_vps", local_open_positions: 0, note: "Handed back." },
  };
  fetchMock = vi.fn(async () => ({
    ok: response.status < 400,
    status: response.status,
    json: async () => response.body,
  }));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const writes = () => fetchMock.mock.calls.filter((c) => c[1]?.method === "PUT");

const renderIt = (props: Partial<Parameters<typeof ActiveTraderControl>[0]> = {}) =>
  render(
    <ActiveTraderControl
      activeTrader="local"
      remoteConnected
      onChanged={() => {}}
      {...props}
    />,
  );

describe("what it shows", () => {
  it("names the machine that is trading", () => {
    renderIt();
    expect(screen.getByRole("button", { name: /LOCAL/ })).toBeInTheDocument();
  });

  it("says REMOTE when the other node has control", () => {
    renderIt({ activeTrader: "remote_vps" });
    expect(screen.getByRole("button", { name: /REMOTE/ })).toBeInTheDocument();
  });

  it("is disabled with a reason when no node is paired, not hidden", () => {
    // "Where did the Local/Remote switch go" is a worse question than "why
    // can't I press this".
    renderIt({ remoteConnected: false });

    const button = screen.getByRole("button", { name: /LOCAL/ });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", expect.stringContaining("Pair one"));
  });

  it("sends the operator to the tab where pairing actually happens", () => {
    // It said "Settings > Node". Node & updates generates the token; the
    // address, the token and the Connect button are on Remote node.
    renderIt({ remoteConnected: false });

    expect(screen.getByRole("button", { name: /LOCAL/ }))
      .toHaveAttribute("title", expect.stringContaining("Settings > Remote node"));
  });
});

describe("switching", () => {
  it("does not switch on the first click", async () => {
    // A mis-click that leaves two nodes active is two sets of engines trading
    // one balance, and nothing on either screen would say so.
    renderIt();

    await userEvent.click(screen.getByRole("button", { name: /LOCAL/ }));

    expect(writes()).toHaveLength(0);
    expect(screen.getByText(/Hand control back/)).toBeInTheDocument();
  });

  it("says what will happen, in order, before it happens", async () => {
    renderIt();

    await userEvent.click(screen.getByRole("button", { name: /LOCAL/ }));

    expect(screen.getByText(/engines stop first/)).toBeInTheDocument();
    expect(screen.getByText(/closes nothing/)).toBeInTheDocument();
  });

  it("describes the other direction differently", async () => {
    renderIt({ activeTrader: "remote_vps" });

    await userEvent.click(screen.getByRole("button", { name: /REMOTE/ }));

    expect(screen.getByText(/stand down first/)).toBeInTheDocument();
    expect(screen.getByText(/nothing changes and it stays the active trader/))
      .toBeInTheDocument();
  });

  it("cancelling sends nothing", async () => {
    renderIt();
    await userEvent.click(screen.getByRole("button", { name: /LOCAL/ }));

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(writes()).toHaveLength(0);
  });

  it("confirming asks for the other node", async () => {
    renderIt();
    await userEvent.click(screen.getByRole("button", { name: /LOCAL/ }));

    await userEvent.click(screen.getByRole("button", { name: "Hand back" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0][0]).toBe("/api/node/active-trader");
    expect(JSON.parse(writes()[0][1].body)).toEqual({ trader: "remote_vps" });
  });

  it("asks for local when the remote node has control", async () => {
    // Negative control: a component that always sent the same target would
    // pass the test above and make the switch one-way.
    renderIt({ activeTrader: "remote_vps" });
    await userEvent.click(screen.getByRole("button", { name: /REMOTE/ }));

    await userEvent.click(screen.getByRole("button", { name: "Take over" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({ trader: "local" });
  });

  it("re-reads the header afterwards", async () => {
    // The button's own label comes from the poll. Without this the operator
    // presses LOCAL, the switch succeeds, and the header still says LOCAL
    // until the next tick.
    const onChanged = vi.fn();
    renderIt({ onChanged });
    await userEvent.click(screen.getByRole("button", { name: /LOCAL/ }));

    await userEvent.click(screen.getByRole("button", { name: "Hand back" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });
});

describe("what it reports", () => {
  it("shows the backend's note, not a generic 'done'", async () => {
    // "2 of its positions keep running to their own SL/TP" is a fact the
    // operator needs and cannot get anywhere else.
    response.body = {
      active_trader: "local",
      note: "Now trading on this machine. The remote node stood down; 2 of its position(s) keep running to their own SL/TP.",
    };
    renderIt({ activeTrader: "remote_vps" });
    await userEvent.click(screen.getByRole("button", { name: /REMOTE/ }));

    await userEvent.click(screen.getByRole("button", { name: "Take over" }));

    expect(await screen.findByRole("status"))
      .toHaveTextContent("2 of its position(s) keep running");
  });

  it("shows a refusal in the backend's own words", async () => {
    // "The VPS did not acknowledge" is the difference between "try again" and
    // "your account is being traded twice".
    response = {
      status: 409,
      body: {
        error: {
          kind: "refusal",
          message: "The remote node did not acknowledge the stand-down. Nothing changed.",
          ref: null,
        },
      },
    };
    renderIt({ activeTrader: "remote_vps" });
    await userEvent.click(screen.getByRole("button", { name: /REMOTE/ }));

    await userEvent.click(screen.getByRole("button", { name: "Take over" }));

    expect(await screen.findByRole("alert"))
      .toHaveTextContent("did not acknowledge the stand-down");
  });

  it("closes the dialog on a refusal rather than leaving it open over the message", async () => {
    response = {
      status: 409,
      body: { error: { kind: "refusal", message: "No.", ref: null } },
    };
    renderIt();
    await userEvent.click(screen.getByRole("button", { name: /LOCAL/ }));

    await userEvent.click(screen.getByRole("button", { name: "Hand back" }));

    await waitFor(() =>
      expect(screen.queryByText(/engines stop first/)).not.toBeInTheDocument());
  });
});

describe("how long it stays (owner, 2026-09-26)", () => {
  // It sits in the title bar. A success left there for good took the space
  // for as long as the app ran; five seconds is enough to read it.
  afterEach(() => vi.useRealTimers());

  const handBack = async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderIt();
    await userEvent.click(screen.getByRole("button", { name: /LOCAL/ }));
    await userEvent.click(screen.getByRole("button", { name: "Hand back" }));
  };

  it("a success goes after five seconds", async () => {
    await handBack();
    expect(await screen.findByRole("status")).toHaveTextContent("Handed back.");

    await act(async () => { await vi.advanceTimersByTimeAsync(5_100); });

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("a success is still there just before five seconds", async () => {
    await handBack();
    await screen.findByRole("status");

    await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });

    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("a refusal stays, because it may mean nothing is trading", async () => {
    response = {
      status: 409,
      body: { error: { kind: "refusal", message: "The remote node did not acknowledge.", ref: null } },
    };
    await handBack();
    await screen.findByRole("alert");

    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });

    expect(screen.getByRole("alert")).toHaveTextContent("did not acknowledge");
  });
});

describe("taking over without the VPS (owner's decision, 2026-09-25)", () => {
  // The VPS became unreachable and the Mac sat on REMOTE with this button
  // disabled: nothing was trading and nothing could be made to trade. The
  // owner chose an explicit way out, behind a confirmation that the VPS is
  // not trading, because a VPS that cannot be reached may still be.
  const openIt = async () => {
    response.body = { active_trader: "local", remote_open_positions: 0,
      note: "Now trading on this machine. It will be told to stand down as soon as it reconnects." };
    renderIt({ activeTrader: "remote_vps", remoteConnected: false });
    await userEvent.click(screen.getByRole("button", { name: /REMOTE/ }));
  };

  it("is offered when this machine is on REMOTE and the VPS is unreachable", async () => {
    await openIt();

    expect(screen.getByRole("dialog")).toHaveTextContent(/Take over without the VPS/);
  });

  it("will not go until the operator confirms the VPS is not trading", async () => {
    await openIt();

    expect(screen.getByRole("button", { name: "Take over" })).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("button", { name: "Take over" })).toBeEnabled();
  });

  it("says what could go wrong before it happens", async () => {
    await openIt();

    expect(screen.getByRole("dialog")).toHaveTextContent(/same account/);
    expect(screen.getByRole("dialog")).toHaveTextContent(/reconnects/);
  });

  it("asks for the forced take-over, and only that", async () => {
    await openIt();
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(screen.getByRole("button", { name: "Take over" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({ trader: "local", without_peer: true });
    expect(await screen.findByRole("status")).toHaveTextContent(/stand down/);
  });

  it("is still disabled for handing back, which needs the VPS to answer", () => {
    renderIt({ activeTrader: "local", remoteConnected: false });

    expect(screen.getByRole("button", { name: /LOCAL/ })).toBeDisabled();
  });
});
