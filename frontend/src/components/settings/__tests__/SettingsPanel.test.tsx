import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsPanel } from "../SettingsPanel";
import { RISK_KEYS } from "../content/risk";

const BODIES: Record<string, unknown> = {
  "/api/settings/risk": {
    risk_per_trade_pct: 1, max_risk_per_trade_pct: 2, max_open_trades: 3,
    max_lot_size: 0.5, max_daily_loss_pct: 5, max_total_drawdown_pct: 10,
    giveback_guard_enabled: 0, giveback_arm_usd: 50, giveback_pct: 40,
    circuit_breaker_enabled: 1, circuit_breaker_losses: 3,
    circuit_breaker_cooldown_mins: 60, internal_hedge_mode: "off",
    internal_net_exposure_max_lots: 0.5, dpm_enabled: 0, profit_close_usd: 0,
    risk_governor_enabled: 0,
  },
  "/api/settings/mt5": { login: "5203117", server: "Vantage-Demo", password_set: true },
  // The shapes the stores actually return. `telegram_config` has three
  // columns and none of them is an api_id; the reader's Telethon credentials
  // are a different store entirely, which is what the first port got wrong.
  "/api/settings/telegram": { bot_token_enc_set: true, chat_id: "-100", enabled: 1 },
  "/api/settings/telegram-reader": {
    telegram_api_id: "12345", telegram_phone: "+44", telegram_api_hash_set: true,
  },
  "/api/settings/email": {
    smtp_host: "smtp.example.com", smtp_port: "587", smtp_password_set: false,
    to_addr: "me@example.com", send_provider: "resend", orb_report_enabled: 1,
    daily_enabled: 0,
  },
  "/api/notifications/test-email": { sent: true, to: "me@example.com" },
  "/api/notifications/test-telegram": { sent: true },
  "/api/settings/expert-params": { re_min_adx: { value: 22, default: 20 } },
  "/api/ai/settings": {
    provider: "claude", providers: ["claude", "deepseek"],
    claude_model: "claude-sonnet-4-6", deepseek_model: "",
    anthropic_api_key_set: true, deepseek_api_key_set: false,
    claude_models: ["claude-sonnet-4-6", "claude-opus-4-8"],
    deepseek_models: ["deepseek-chat"], configured: true,
  },
  "/api/ai/settings/test": { provider: "claude", ok: true, billable: true,
                             note: "claude answered." },
  "/api/ai/settings/models": { provider: "claude", models: ["a", "b", "c"] },
  "/api/settings/access": { auto_login: false, warning: "" },
  "/api/node/licence": {
    email: "simon@example.com", licence_type: "perpetual",
    expiry_date: "2030-01-01", machine_id: "abc123",
    key_masked: "ABCD1234 - **** - ****",
  },
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
  fetchMock = vi.fn(async (url: string) => {
    const key = String(url);
    const body = (overrides[key] ?? BODIES[key] ?? {}) as Record<string, unknown>;
    // An override may carry `__status` to make the endpoint refuse. Without
    // it every test here would be a happy path, and the one thing a test-send
    // button exists for is what it says when the send FAILS.
    const status = Number(body.__status ?? 200);
    return { ok: status < 400, status, json: async () => body };
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const writes = () => fetchMock.mock.calls.filter((c) => c[1]?.method && c[1].method !== "GET");

describe("risk", () => {
  it("shows the stored numbers, under the names the column actually has", async () => {
    // The first React version of this tab offered `risk_pct` and
    // `daily_loss_limit_pct`. Neither is a column of vantage_risk_settings, so
    // both raised on save and the value was silently lost while the box kept
    // showing what was typed.
    render(<SettingsPanel />);

    expect(await screen.findByLabelText("Risk per trade (%)")).toHaveValue("1");
    expect(screen.getByLabelText("Maximum open trades")).toHaveValue("3");
    expect(screen.getByLabelText("Max daily loss (%)")).toHaveValue("5");
  });

  it("saves a field when it is left, not on every keystroke", async () => {
    render(<SettingsPanel />);
    const field = await screen.findByLabelText("Risk per trade (%)");

    await userEvent.clear(field);
    await userEvent.type(field, "2");
    expect(writes()).toHaveLength(0);

    await userEvent.tab();

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({ risk_per_trade_pct: 2 });
  });

  it("adopts the value the backend stored, not the one that was typed", async () => {
    // The risk service clamps. A field that kept the operator's number would
    // tell them the engine is using a value it is not.
    overrides["/api/settings/risk"] = {
      ...BODIES["/api/settings/risk"] as object, risk_per_trade_pct: 2,
    };
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

  it("offers every setting the store has a column for, not four of them", async () => {
    // The tab shipped with 4 fields where the NiceGUI page had 22. An operator
    // cannot set a give-back limit, a circuit breaker or an exposure cap from a
    // screen that does not show them.
    render(<SettingsPanel />);
    await screen.findByLabelText("Risk per trade (%)");

    for (const key of RISK_KEYS) {
      expect(screen.getByTestId(`risk-${key}`)).toBeInTheDocument();
    }
    expect(RISK_KEYS.length).toBeGreaterThanOrEqual(20);
  });

  it("saves a toggle as the 0/1 the column holds", async () => {
    render(<SettingsPanel />);

    await userEvent.click(await screen.findByLabelText("Risk governor"));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({ risk_governor_enabled: 1 });
  });

  it("saves the hedging mode as the string the column holds", async () => {
    render(<SettingsPanel />);

    await userEvent.selectOptions(await screen.findByLabelText("Hedging"), "net_exposure");

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({ internal_hedge_mode: "net_exposure" });
  });

  it("says when a setting does nothing because its switch is off", async () => {
    // "I turned it on and it made no difference" is how a setting gets
    // reported as broken.
    render(<SettingsPanel />);
    await screen.findByLabelText("Risk per trade (%)");

    expect(within(screen.getByTestId("risk-giveback_arm_usd"))
      .getByText(/Does nothing until the give-back guard is on/)).toBeInTheDocument();
  });

  it("stops saying so once the switch is on", async () => {
    // Negative control: a note shown always is a note nobody reads.
    overrides["/api/settings/risk"] = {
      ...BODIES["/api/settings/risk"] as object, giveback_guard_enabled: 1,
    };
    render(<SettingsPanel />);
    await screen.findByLabelText("Risk per trade (%)");

    expect(within(screen.getByTestId("risk-giveback_arm_usd"))
      .queryByText(/Does nothing until/)).not.toBeInTheDocument();
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
  const openTab = async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Connections" }));
  };

  it("shows a stored secret as stored, with an empty field", async () => {
    await openTab();

    const hash = await screen.findByLabelText("API hash");
    expect(hash).toHaveValue("");
    expect(hash.closest("label")).toHaveTextContent("stored; leave blank to keep it");
  });

  it("says when a secret has never been set", async () => {
    await openTab();

    // Scoped to the field. Several secrets are unset on this screen, and
    // "some element somewhere says 'not set'" is not the claim.
    const field = await screen.findByLabelText("SMTP password");
    expect(field.closest("label")).toHaveTextContent("not set");
  });

  it("writes each domain to its own endpoint", async () => {
    await openTab();

    const host = await screen.findByLabelText("SMTP host");
    await userEvent.clear(host);
    await userEvent.type(host, "smtp.new.com");
    await userEvent.tab();

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0][0]).toBe("/api/settings/email");
  });

  it("reports a bot token as stored even though it is written under another name", async () => {
    // Written as `bot_token`, stored as `bot_token_enc`. Reading the flag
    // under the write name reports a configured bot as "not set", which reads
    // as "your alerts are broken" when they are fine.
    await openTab();

    const token = await screen.findByLabelText("Bot token");

    expect(token.closest("label")).toHaveTextContent("stored; leave blank to keep it");
  });

  it("writes the Telethon credentials to the reader endpoint, not the bot's", async () => {
    // These are config.yaml keys. The first port sent all three to the alert
    // bot's table, which has no such columns, so every save was a 500.
    await openTab();

    const apiId = await screen.findByLabelText("API ID");
    await userEvent.clear(apiId);
    await userEvent.type(apiId, "999");
    await userEvent.tab();

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0][0]).toBe("/api/settings/telegram-reader");
    expect(JSON.parse(writes()[0][1].body)).toEqual({ telegram_api_id: "999" });
  });

  it("sends the address under the name the column has", async () => {
    // `recipient` is not a column; `to_addr` is. The write raised and the
    // address was silently lost.
    await openTab();

    const to = await screen.findByLabelText("Send reports to");
    expect(to).toHaveValue("me@example.com");

    await userEvent.clear(to);
    await userEvent.type(to, "new@example.com");
    await userEvent.tab();

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({ to_addr: "new@example.com" });
  });

  it("saves a schedule toggle as the 0/1 the column holds", async () => {
    await openTab();

    await userEvent.click(await screen.findByLabelText("Daily summary"));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({ daily_enabled: 1 });
  });

  it("shows a stored toggle as on", async () => {
    // Negative control for the one above: a renderer that ignored the stored
    // value would pass it and show every switch off.
    await openTab();

    expect(await screen.findByLabelText("Morning ORB / IVB report")).toBeChecked();
    expect(screen.getByLabelText("Daily summary")).not.toBeChecked();
  });

  it("tests delivery through the provider on screen, without saving it", async () => {
    await openTab();

    await userEvent.selectOptions(
      await screen.findByLabelText("Send reports via"), "gmail",
    );
    await waitFor(() => expect(writes()).toHaveLength(1));

    await userEvent.click(screen.getByRole("button", { name: "Test delivery" }));

    await waitFor(() => expect(writes()).toHaveLength(2));
    expect(writes()[1][0]).toBe("/api/notifications/test-email");
    expect(writes()[1][1].method).toBe("POST");
  });

  it("reports where a test email went", async () => {
    await openTab();

    await userEvent.click(
      await screen.findByRole("button", { name: "Test delivery" }));

    expect(await screen.findByRole("status")).toHaveTextContent("me@example.com");
  });

  it("shows a refusal's own words rather than 'failed'", async () => {
    // The backend translates `535 5.7.139` into the five-click Outlook fix.
    // Summarising it here would throw away the only useful part.
    overrides["/api/notifications/test-email"] = {
      __status: 409,
      error: {
        kind: "refusal",
        message: "Microsoft rejected the login: SMTP AUTH is disabled.",
        ref: null,
      },
    };
    await openTab();

    await userEvent.click(
      await screen.findByRole("button", { name: "Test delivery" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("SMTP AUTH is disabled");
  });

  it("offers the ORB report and the Telegram alert as their own sends", async () => {
    await openTab();

    await userEvent.click(
      await screen.findByRole("button", { name: "Send test ORB report" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0][0]).toBe("/api/notifications/test-orb-report");

    await userEvent.click(screen.getByRole("button", { name: "Test alert" }));
    await waitFor(() => expect(writes()).toHaveLength(2));
    expect(writes()[1][0]).toBe("/api/notifications/test-telegram");
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

describe("remote node", () => {
  const REMOTE = {
    server: { enabled: false, port: 8765, running: false, fingerprint: "AA:BB", token_set: true },
    client: {
      host: "10.0.0.5", port: 8765, token_set: true,
      conn_state: "disconnected", last_error: "refused", remote_status: { balance: 1000 },
    },
    headless: false,
    centralized_signal_gen: false,
  };

  const openTab = async () => {
    overrides["/api/remote/state"] = { ...REMOTE };
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Remote node" }));
  };

  it("shows both roles and the live link state", async () => {
    await openTab();

    expect(await screen.findByText(/accepts connections/)).toBeInTheDocument();
    expect(screen.getByLabelText("VPS address")).toHaveValue("10.0.0.5");
    expect(screen.getByText(/disconnected/)).toBeInTheDocument();
  });

  it("does not show the peer's balance while the link is down", async () => {
    // A number left on screen from a link that has since dropped is a number
    // the operator will act on.
    await openTab();

    await screen.findByLabelText("VPS address");
    expect(screen.queryByText(/VPS balance/)).not.toBeInTheDocument();
  });

  it("shows the peer's numbers once connected", async () => {
    overrides["/api/remote/state"] = {
      ...REMOTE,
      client: { ...REMOTE.client, conn_state: "connected" },
    };
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Remote node" }));

    expect(await screen.findByText(/VPS balance/)).toBeInTheDocument();
  });

  it("saves and connects in one action", async () => {
    await openTab();

    await userEvent.type(await screen.findByLabelText("Shared token"), "tok");
    await userEvent.click(screen.getByRole("button", { name: "Save and connect" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0][0]).toBe("/api/remote/client");
    expect(JSON.parse(writes()[0][1].body)).toEqual({
      host: "10.0.0.5", port: 8765, token: "tok",
    });
  });

  it("never puts a stored token back on screen", async () => {
    await openTab();

    expect(await screen.findByLabelText("Shared token")).toHaveValue("");
    expect(screen.getByText("one is stored; re-enter it to reconnect")).toBeInTheDocument();
  });

  it("will not transfer models while disconnected, and says why", async () => {
    await openTab();

    const button = await screen.findByRole("button", { name: /Download from VPS/ });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", expect.stringContaining("Not connected"));
  });

  it("warns what centralized signal generation costs, before it is flicked", async () => {
    // An operator who turns this on and shuts the laptop has stopped trading
    // without meaning to.
    await openTab();

    expect(await screen.findByText(/does not fall back to generating its own/))
      .toBeInTheDocument();
  });

  it("shows the backend's note after a switch, not a generic 'saved'", async () => {
    overrides["/api/remote/centralized-signals"] = {
      centralized_signal_gen: true,
      note: "This machine is now the only source of new signals.",
    };
    await openTab();

    await userEvent.click(
      await screen.findByLabelText("Generate signals on this node only"));

    expect(await screen.findByRole("status"))
      .toHaveTextContent("only source of new signals");
  });

  it("shows a refusal in the backend's own words", async () => {
    overrides["/api/remote/server"] = {
      __status: 409,
      error: { kind: "refusal", message: "Generate a pairing token first.", ref: null },
    };
    await openTab();

    await userEvent.click(
      await screen.findByLabelText("Accept remote connections"));

    expect(await screen.findByRole("alert"))
      .toHaveTextContent("Generate a pairing token first");
  });
});

describe("AI", () => {
  /**
   * Restored 2026-09-18. Without this tab there was no way to enter an API key
   * from the dashboard, so on a fresh install every AI feature in the app was
   * unreachable unless somebody edited config.yaml by hand.
   */
  const open = async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "AI" }));
  };

  it("shows the provider and its model", async () => {
    await open();

    expect(await screen.findByLabelText("AI provider")).toHaveValue("claude");
    expect(screen.getByLabelText("Model")).toHaveValue("claude-sonnet-4-6");
  });

  it("never puts a stored key back on screen", async () => {
    await open();

    expect(await screen.findByLabelText("API key")).toHaveValue("");
    expect(screen.getByText("stored; leave blank to keep it")).toBeInTheDocument();
  });

  it("says when no key is stored", async () => {
    overrides["/api/ai/settings"] = {
      ...BODIES["/api/ai/settings"] as object,
      provider: "deepseek", deepseek_api_key_set: false,
    };
    await open();

    expect(await screen.findByText("not set")).toBeInTheDocument();
  });

  it("warns when nothing is configured at all", async () => {
    // Otherwise the Analysis tab just returns nothing and the reason is three
    // screens away.
    overrides["/api/ai/settings"] = {
      ...BODIES["/api/ai/settings"] as object, configured: false,
    };
    await open();

    expect(await screen.findByText(/No AI provider is configured/)).toBeInTheDocument();
  });

  it("tests the key that was TYPED, so it can be checked before it is saved", async () => {
    // Saving first and finding out hours later that an analysis failed is the
    // version this replaces.
    await open();

    await userEvent.type(await screen.findByLabelText("API key"), "sk-new");
    await userEvent.click(screen.getByRole("button", { name: "Test connection" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0][0]).toBe("/api/ai/settings/test");
    expect(JSON.parse(writes()[0][1].body)).toEqual({
      provider: "claude", api_key: "sk-new",
    });
  });

  it("says testing costs something", async () => {
    await open();

    expect(await screen.findByText(/costs five tokens/)).toBeInTheDocument();
  });

  it("will not save an empty key, and says why", async () => {
    await open();

    const save = await screen.findByRole("button", { name: "Save key" });
    expect(save).toBeDisabled();
    expect(save).toHaveAttribute("title", expect.stringContaining("Type a key"));
  });

  it("keeps showing a stored model the fetched list no longer has", async () => {
    // A model this key can no longer use is a real state. A dropdown that
    // quietly showed something else would have the operator believe they are
    // on a model they are not.
    overrides["/api/ai/settings"] = {
      ...BODIES["/api/ai/settings"] as object,
      claude_model: "claude-retired", claude_models: ["claude-sonnet-4-6"],
    };
    await open();

    expect(await screen.findByLabelText("Model")).toHaveValue("claude-retired");
  });
});

describe("access and licence", () => {
  const open = async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Access & licence" }));
  };

  it("shows which way the password prompt is set", async () => {
    await open();

    expect(await screen.findByLabelText("Ask for the dashboard password")).toBeChecked();
    expect(screen.getByLabelText("Log in automatically")).not.toBeChecked();
  });

  it("shows the backend's warning when automatic login is on", async () => {
    // The one thing the operator has to weigh. Composing it in the browser
    // would mean a UI that forgot it offers the choice without the consequence.
    overrides["/api/settings/access"] = {
      auto_login: true,
      warning: "Anyone who can open this machine can place and close live trades without a password.",
    };
    await open();

    expect(await screen.findByRole("alert"))
      .toHaveTextContent("place and close live trades without a password");
  });

  it("says nothing alarming when the password is required", async () => {
    await open();
    await screen.findByLabelText("Ask for the dashboard password");

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("saves the choice", async () => {
    await open();

    await userEvent.click(await screen.findByLabelText("Log in automatically"));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0][0]).toBe("/api/settings/access");
    expect(JSON.parse(writes()[0][1].body)).toEqual({ auto_login: true });
  });

  it("shows the licence, with the key already masked", async () => {
    await open();

    expect(await screen.findByText("simon@example.com")).toBeInTheDocument();
    expect(screen.getByText("abc123")).toBeInTheDocument();
    expect(screen.getByText("ABCD1234 - **** - ****")).toBeInTheDocument();
  });
});
