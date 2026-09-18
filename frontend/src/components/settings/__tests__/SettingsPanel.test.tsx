import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsPanel } from "../SettingsPanel";

const BODIES: Record<string, unknown> = {
  "/api/settings/risk": { risk_pct: 1, max_open_trades: 3, daily_loss_limit_pct: 5, max_lot_size: 0.5 },
  "/api/settings/mt5": { login: "5203117", server: "Vantage-Demo", password_set: true },
  "/api/settings/telegram": { api_id: "12345", phone: "+44", api_hash_set: true },
  "/api/settings/email": { smtp_host: "smtp.example.com", smtp_port: "587", smtp_password_set: false },
  "/api/settings/expert-params": { re_min_adx: { value: 22, default: 20 } },
  "/api/node/state": {
    version: "1.4.2",
    active_trader: "local",
    sync_token_set: true,
    registration: { approved: true },
    registered_email: "simon@example.com",
    autostart: { supported: true, installed: false, armed: false, check_interval_secs: 300 },
  },
  "/api/node/update": { current: "1.4.2", update: null, changes: [] },
  "/api/node/sync-token": {},
  "/api/settings/diagnostics": {
    log: [["12:00:01", "Engine started"]],
    circuit_breaker: { tripped: true, reason: "3 consecutive losses" },
  },
};

let fetchMock: ReturnType<typeof vi.fn>;
let overrides: Record<string, unknown>;

beforeEach(() => {
  overrides = {};
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const key = String(url);
    if (init?.method && init.method !== "GET") {
      return { ok: true, status: 200, json: async () => overrides[key] ?? BODIES[key] ?? {} };
    }
    return { ok: true, status: 200, json: async () => overrides[key] ?? BODIES[key] ?? {} };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const writes = () => fetchMock.mock.calls.filter((c) => c[1]?.method && c[1].method !== "GET");

describe("risk", () => {
  it("shows the stored numbers", async () => {
    render(<SettingsPanel />);

    expect(await screen.findByLabelText("Risk per trade (%)")).toHaveValue("1");
    expect(screen.getByLabelText("Maximum open trades")).toHaveValue("3");
  });

  it("saves a field when it is left, not on every keystroke", async () => {
    render(<SettingsPanel />);
    const field = await screen.findByLabelText("Risk per trade (%)");

    await userEvent.clear(field);
    await userEvent.type(field, "2");
    expect(writes()).toHaveLength(0);

    await userEvent.tab();

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({ risk_pct: 2 });
  });

  it("adopts the value the backend stored, not the one that was typed", async () => {
    // The risk service clamps. A field that kept the operator's number would
    // tell them the engine is using a value it is not.
    overrides["/api/settings/risk"] = { ...BODIES["/api/settings/risk"] as object, risk_pct: 2 };
    render(<SettingsPanel />);
    const field = await screen.findByLabelText("Risk per trade (%)");

    await userEvent.clear(field);
    await userEvent.type(field, "99");
    await userEvent.tab();

    await waitFor(() => expect(field).toHaveValue("2"));
  });

  it("says these decide how much a trade can lose", async () => {
    render(<SettingsPanel />);

    expect(await screen.findByText(/how much a single trade can lose/)).toBeInTheDocument();
  });
});

describe("MT5", () => {
  it("shows which account is configured without showing a password", async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "MT5" }));

    expect(await screen.findByText(/5203117/)).toBeInTheDocument();
    expect(screen.getByText(/never sent to this screen/)).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });

  it("says when no password is stored, because the bridge cannot log in", async () => {
    overrides["/api/settings/mt5"] = { login: "5203117", server: "X", password_set: false };
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "MT5" }));

    expect(await screen.findByText(/cannot log in/)).toBeInTheDocument();
  });

  it("will not save a half-filled form, and says what is missing", async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "MT5" }));

    const save = await screen.findByRole("button", { name: /Save and sync/ });
    expect(save).toBeDisabled();
    expect(save).toHaveAttribute("title", expect.stringContaining("login, password and server"));
  });

  it("says saving also pushes to the bridge", async () => {
    // Saved and not synced leaves the bridge logged in as the previous account.
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "MT5" }));

    expect(await screen.findByText(/pushes them to the bridge/)).toBeInTheDocument();
  });
});

describe("connections", () => {
  it("shows a stored secret as stored, with an empty field", async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Connections" }));

    const hash = await screen.findByLabelText("API hash");
    expect(hash).toHaveValue("");
    expect(screen.getByText("stored; leave blank to keep it")).toBeInTheDocument();
  });

  it("says when a secret has never been set", async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Connections" }));

    await screen.findByLabelText("Password");
    expect(screen.getByText("not set")).toBeInTheDocument();
  });

  it("writes each domain to its own endpoint", async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Connections" }));

    const host = await screen.findByLabelText("SMTP host");
    await userEvent.clear(host);
    await userEvent.type(host, "smtp.new.com");
    await userEvent.tab();

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0][0]).toBe("/api/settings/email");
  });
});

describe("expert tunables", () => {
  it("renders whatever the catalogue lists, with no per-parameter code", async () => {
    // `/add-tunable` exists so a new tunable appears here on its own.
    overrides["/api/settings/expert-params"] = {
      re_min_adx: { value: 22, default: 20 },
      a_brand_new_tunable: { value: 7, default: 5 },
    };
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Expert tunables" }));

    expect(await screen.findByTestId("tunable-re_min_adx")).toBeInTheDocument();
    expect(screen.getByTestId("tunable-a_brand_new_tunable")).toBeInTheDocument();
    expect(screen.getByLabelText("a_brand_new_tunable")).toHaveValue("7");
  });

  it("shows each parameter's default", async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Expert tunables" }));

    expect(await screen.findByText("default 20")).toBeInTheDocument();
  });
});

describe("diagnostics", () => {
  it("shows a tripped breaker and why", async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Diagnostics" }));

    const breaker = await screen.findByTestId("circuit-breaker");
    expect(breaker).toHaveAttribute("data-tripped", "true");
    expect(breaker).toHaveTextContent("3 consecutive losses");
  });

  it("only offers a reset when there is something to reset", async () => {
    overrides["/api/settings/diagnostics"] = { log: [], circuit_breaker: { tripped: false } };
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Diagnostics" }));

    const reset = await screen.findByRole("button", { name: "Reset" });
    expect(reset).toBeDisabled();
    expect(reset).toHaveAttribute("title", "The breaker is not tripped.");
  });

  it("resets the breaker when asked", async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Diagnostics" }));

    await userEvent.click(await screen.findByRole("button", { name: "Reset" }));

    await waitFor(() => {
      expect(writes().some((c) => c[0] === "/api/settings/circuit-breaker/reset")).toBe(true);
    });
  });

  it("shows the log since the app started", async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Diagnostics" }));

    expect(await screen.findByText("Engine started")).toBeInTheDocument();
  });
});


describe("node and updates", () => {
  it("says a token is stored without ever showing it", async () => {
    // It is shown exactly once, when it is created. This screen can only
    // honestly report whether one exists.
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Node & updates" }));

    expect(await screen.findByText(/never shown again/)).toBeInTheDocument();
    expect(screen.queryByTestId("new-sync-token")).toBeNull();
  });

  it("shows a newly generated token once, with the warning", async () => {
    overrides["/api/node/sync-token"] = {
      token: "brand-new-token",
      note: "Copy this into the other node now. It replaces any previous token.",
    };
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Node & updates" }));

    await userEvent.click(await screen.findByRole("button", { name: /Generate a new token/ }));

    const shown = await screen.findByTestId("new-sync-token");
    expect(shown).toHaveTextContent("brand-new-token");
    expect(shown).toHaveTextContent("replaces any previous token");
  });

  it("says an unpaired node is unpaired", async () => {
    overrides["/api/node/state"] = {
      ...(BODIES["/api/node/state"] as object), sync_token_set: false,
    };
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Node & updates" }));

    expect(await screen.findByText(/not paired/)).toBeInTheDocument();
  });

  it("will not request approval without an email, and says why", async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Node & updates" }));

    const request = await screen.findByRole("button", { name: /Request approval/ });
    expect(request).toBeDisabled();
    expect(request).toHaveAttribute("title", expect.stringContaining("email address"));
  });

  it("says the app is up to date rather than leaving it blank", async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Node & updates" }));

    expect(await screen.findByText(/up to date/)).toBeInTheDocument();
  });

  it("will not offer to apply an update that does not exist", async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Node & updates" }));

    const apply = await screen.findByRole("button", { name: /Apply the update/ });
    expect(apply).toBeDisabled();
    expect(apply).toHaveAttribute("title", "There is no update to apply.");
  });

  it("offers to apply one when there is", async () => {
    overrides["/api/node/update"] = {
      current: "1.4.2", update: { version: "1.5.0" }, changes: ["Ported the News tab"],
    };
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Node & updates" }));

    expect(await screen.findByText(/1.5.0 is available/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Apply the update/ })).toBeEnabled();
  });

  it("says when autostart is not supported rather than offering a dead switch", async () => {
    overrides["/api/node/state"] = {
      ...(BODIES["/api/node/state"] as object),
      autostart: { supported: false, installed: false, armed: false },
    };
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Node & updates" }));

    expect(await screen.findByText("Not supported on this platform.")).toBeInTheDocument();
    expect(screen.queryByLabelText(/Start the app when this machine boots/)).toBeNull();
  });
});
