import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { KeepAliveSection } from "../internal/KeepAliveSection";

/**
 * "Keep it running" — the OS-level watchdog.
 *
 * Owner, 2026-09-21: "in settings it is missing the toggle to ensure the app,
 * bridge and mt5 is kept alive, turning it on keeps everything running and
 * uses a watchdog to check".
 *
 * It was not missing; it was reported by half and labelled as something else.
 * The React port rendered one checkbox called "Start the app when this
 * machine boots", bound to `installed` — whether the OS scheduler entry
 * exists. The NiceGUI page it replaced reported four states, and the
 * difference between them is the whole value of a supervision feature:
 *
 * - **off** — nothing will restart it
 * - **on and installed and armed** — checking every N minutes
 * - **on and installed but disarmed** — the operator ran the Stop script;
 *   supervision is deliberately paused and that is not a fault
 * - **on but NOT installed** — the entry was lost (an OS upgrade, a machine
 *   migration). This is the one that matters, and an unticked box is exactly
 *   the wrong way to show it.
 */
const ON = { supported: true, enabled: true, installed: true, armed: true,
             check_interval_secs: 120 };

function renderIt(over: Record<string, unknown> = {}) {
  const onChange = vi.fn(async () => {});
  render(<KeepAliveSection autostart={{ ...ON, ...over }} onChange={onChange} />);
  return { onChange };
}

describe("what it says is happening", () => {
  it("says it is checking, and how often", async () => {
    renderIt();

    expect(screen.getByRole("status")).toHaveTextContent(/every 2 min/i);
  });

  it("says nothing will restart it when the feature is off", () => {
    renderIt({ enabled: false, installed: false, armed: false });

    expect(screen.getByRole("status")).toHaveTextContent(/nothing will restart/i);
  });

  it("calls a deliberate Stop a pause, not a fault", () => {
    // `FOREX Stop.command` disarms the watchdog so that Stop genuinely
    // stops. Reporting that as broken would train the operator to ignore the
    // line that reports the real fault below.
    renderIt({ armed: false });

    const said = screen.getByRole("status");
    expect(said).toHaveTextContent(/paused/i);
    expect(said).not.toHaveTextContent(/missing/i);
  });

  it("warns when the toggle is on but the scheduler entry is gone", () => {
    renderIt({ installed: false, armed: false });

    const said = screen.getByRole("status");
    expect(said).toHaveTextContent(/missing/i);
    expect(said).toHaveTextContent(/off and on/i);
  });

  it("says so on a platform that cannot do this at all", () => {
    renderIt({ supported: false });

    expect(screen.getByRole("status")).toHaveTextContent(/not supported/i);
    expect(screen.queryByRole("checkbox")).toBeNull();
  });
});

describe("the switch", () => {
  it("follows the stored setting, not whether the OS entry happens to exist", async () => {
    // Bound to `installed`, a lost scheduler entry renders as "the operator
    // never turned this on" — which is the bug this section replaced.
    renderIt({ installed: false, armed: false });

    expect(screen.getByRole("checkbox")).toBeChecked();
  });

  it("turns it on", async () => {
    const { onChange } = renderIt({ enabled: false, installed: false, armed: false });

    await userEvent.click(screen.getByRole("checkbox"));

    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("turns it off", async () => {
    const { onChange } = renderIt();

    await userEvent.click(screen.getByRole("checkbox"));

    expect(onChange).toHaveBeenCalledWith(false);
  });

  it("shows the reason when the OS refuses", async () => {
    const onChange = vi.fn(async () => { throw new Error("launchctl: Operation not permitted"); });
    render(<KeepAliveSection autostart={{ ...ON, enabled: false, installed: false }}
                             onChange={onChange} />);

    await userEvent.click(screen.getByRole("checkbox"));

    expect(await screen.findByRole("alert"))
      .toHaveTextContent(/Operation not permitted/);
  });

  it("does not claim it is on when the OS refused", async () => {
    // A toggle showing ON with no scheduler entry behind it is the false
    // sense of safety this whole feature exists to remove.
    const onChange = vi.fn(async () => { throw new Error("nope"); });
    render(<KeepAliveSection autostart={{ ...ON, enabled: false, installed: false, armed: false }}
                             onChange={onChange} />);

    await userEvent.click(screen.getByRole("checkbox"));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("checkbox")).not.toBeChecked();
  });
});

describe("what it tells the operator it covers", () => {
  it("names the app, the bridge and MT5", () => {
    // The owner asked for "the app, bridge and mt5 kept alive". The OS
    // watchdog restarts the APP; the app's own start-up brings the bridge and
    // MT5 back with it. Saying so is the difference between trusting this and
    // wondering what it actually watches.
    renderIt();

    const about = screen.getByTestId("keep-alive-explains");
    expect(about).toHaveTextContent(/bridge/i);
    expect(about).toHaveTextContent(/MT5/i);
  });
});

describe("wired into the Node tab", () => {
  /**
   * The toggle this replaced PUT to `/api/node/state`, which has no PUT
   * handler — it answered 405 and `useSettingsResource.save` swallowed that
   * into its own error field, so the box moved, nothing was written, and no
   * message reached the screen. The endpoint is `/api/node/autostart`.
   */
  const STATE = {
    version: "0.5", active_trader: "local", sync_token_set: false,
    registered_email: "", registration: {},
    autostart: { supported: true, enabled: false, installed: false,
                 armed: false, check_interval_secs: 120 },
  };

  it("writes to the autostart endpoint, not to the state read", async () => {
    const calls: [string, RequestInit | undefined][] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push([String(url), init]);
      return { ok: true, status: 200,
               headers: { get: () => "application/json" },
               json: async () => (String(url).includes("/api/node/state")
                 ? STATE
                 : { supported: true, installed: true, armed: true }) };
    }));
    const { NodeTab } = await import("../tabs/NodeTab");
    render(<NodeTab />);

    await userEvent.click(
      await screen.findByLabelText(/Keep the app, the bridge and MT5 running/i));

    const written = calls.filter(([, init]) => init?.method && init.method !== "GET");
    expect(written).toHaveLength(1);
    expect(written[0][0]).toBe("/api/node/autostart");
    expect(JSON.parse(String(written[0][1]?.body))).toEqual({ enabled: true });
    vi.unstubAllGlobals();
  });
});
