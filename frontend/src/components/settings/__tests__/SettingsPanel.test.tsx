import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsPanel } from "../SettingsPanel";

const BODIES: Record<string, unknown> = {
  "/api/settings/mt5": {
    // The machine these tests describe is a Mac: the bridge section is
    // macOS-only (2026-09-26).
    platform: "darwin",
    login: "5203117", server: "Vantage-Demo", password_enc_set: true,
    live_login: "900123", live_server: "Vantage-Live", live_password_enc_set: false,
  },
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
  // The real catalogue shape: group name -> list of parameter rows. This
  // fixture was `{ re_min_adx: { value: 22, default: 20 } }`, which the
  // endpoint has never returned, and the tab read it the same invented way --
  // so on the running app it showed four empty boxes named after the GROUPS
  // and hid every actual tunable. Corrected 2026-09-20.
  "/api/settings/expert-params": {
    "Risk filters": [
      { key: "re_min_adx", label: "Minimum ADX", value: 22, default: 20,
        min: 5, max: 60, unit: "", desc: "Below this the trend is too weak." },
    ],
  },
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
    // The real shape. This said { approved: true }; there is no `approved`
    // key, so the card told every install it had not been approved.
    registration: {
      connected: true, last_error: "", version: "1.4.2", latest_version: "",
      update_available: false, changelog: [], connected_host: "",
      subscription_type: "Annual", subscription_expiry: "2027-01-15",
      email: "simon@example.com", nickname: "", is_remote_admin: false,
      registration_sent_at: 0,
    },
    registered_email: "simon@example.com",
    autostart: { supported: true, installed: false, armed: false, check_interval_secs: 300 },
  },
  // The real shape of `GET /api/node/update`. This fixture said
  // `update: null`, which the endpoint never returns -- it always returns the
  // check object, with an `available` flag inside it. That mattered: the
  // component treated the presence of the object as "an update exists", so
  // against the real backend it always claimed one and always enabled the
  // destructive Apply button. Corrected 2026-09-20.
  "/api/node/update": {
    current: "1.4.2",
    update: { available: false, local_sha: "aaaaaaa", remote_sha: "aaaaaaa",
              commits: [], error: null },
    changes: [], changes_error: "",
  },
  "/api/node/sync-token": {},
  "/api/settings/diagnostics": {
    log: [["12:00:01", "Engine started"]],
    // The real shape. This invented `{tripped, reason}`, which the endpoint
    // has never returned -- so the card's `breaker["tripped"]` was always
    // undefined and it reported "clear" whatever the breaker was doing.
    // Corrected 2026-09-20.
    circuit_breaker: {
      enabled: true, losses_threshold: 3, cooldown_mins: 60,
      active_until: 1_789_900_000, consec_losses: 3, is_active: true,
      remaining_secs: 1_500,
    },
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

describe("MT5", () => {
  const open = async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "MT5" }));
  };

  it("shows which account is configured without showing a password", async () => {
    await open();

    expect(await screen.findByText(/5203117/)).toBeInTheDocument();
    expect(screen.getByText(/never sent to this screen/)).toBeInTheDocument();
    expect(screen.getByLabelText("Demo account password")).toHaveValue("");
  });

  it("offers the LIVE account too, which could not be configured at all before", async () => {
    // Without it the demo/live switch can never be used: it refuses to switch
    // to an account it has no credentials for.
    await open();

    expect(await screen.findByTestId("mt5-live")).toBeInTheDocument();
    expect(screen.getByLabelText("Live account login")).toBeInTheDocument();
  });

  it("says when no password is stored, because the bridge cannot log in", async () => {
    await open();

    const live = within(await screen.findByTestId("mt5-live"));
    expect(live.getByText(/cannot log in/)).toBeInTheDocument();
  });

  it("saves the live account under the live environment", async () => {
    await open();

    await userEvent.type(await screen.findByLabelText("Live account login"), "900123");
    await userEvent.type(screen.getByLabelText("Live account password"), "pw");
    await userEvent.type(screen.getByLabelText("Live account server"), "Vantage-Live");
    await userEvent.click(screen.getByRole("button", { name: /Save live credentials/ }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body)).toEqual({
      login: "900123", password: "pw", server: "Vantage-Live", environment: "live",
    });
  });

  it("saves the demo account as demo", async () => {
    // Negative control: a form that always sent one environment would pass the
    // test above and quietly overwrite the wrong account.
    await open();

    await userEvent.type(await screen.findByLabelText("Demo account login"), "5203117");
    await userEvent.type(screen.getByLabelText("Demo account password"), "pw");
    await userEvent.type(screen.getByLabelText("Demo account server"), "Vantage-Demo");
    await userEvent.click(screen.getByRole("button", { name: /Save demo credentials/ }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(JSON.parse(writes()[0][1].body).environment).toBe("demo");
  });

  it("will not save a half-filled form, and says what is missing", async () => {
    await open();

    const save = await screen.findByRole("button", { name: /Save demo credentials/ });
    expect(save).toBeDisabled();
    expect(save).toHaveAttribute("title", expect.stringContaining("login, password and server"));
  });

  it("offers the EA bridge switch", async () => {
    await open();

    expect(await screen.findByLabelText("Use the EA bridge")).toBeInTheDocument();
  });

  it("offers the macOS bridge backend, which had no screen at all", async () => {
    await open();

    expect(await screen.findByLabelText("Backend")).toHaveValue("crossover");
  });

  it("saves a changed backend", async () => {
    await open();

    await userEvent.selectOptions(await screen.findByLabelText("Backend"), "wine");

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0][0]).toBe("/api/settings/app");
    expect(JSON.parse(writes()[0][1].body)).toEqual({ bridge_backend: "wine" });
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
    // `/add-tunable` exists so a new tunable appears here on its own -- in
    // whatever group the catalogue puts it, including a brand new group.
    overrides["/api/settings/expert-params"] = {
      "Risk filters": [
        { key: "re_min_adx", label: "Minimum ADX", value: 22, default: 20,
          min: 5, max: 60, unit: "", desc: "" },
      ],
      "A brand new group": [
        { key: "a_brand_new_tunable", label: "A brand new tunable", value: 7,
          default: 5, min: 1, max: 9, unit: "", desc: "" },
      ],
    };
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Expert tunables" }));

    expect(await screen.findByTestId("tunable-re_min_adx")).toBeInTheDocument();
    expect(screen.getByTestId("tunable-a_brand_new_tunable")).toBeInTheDocument();
    expect(screen.getByLabelText("A brand new tunable")).toHaveValue("7");
    expect(screen.getByRole("heading", { name: "A brand new group" }))
      .toBeInTheDocument();
  });

  it("shows each parameter's default", async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Expert tunables" }));

    expect(await screen.findByTestId("tunable-hint-re_min_adx"))
      .toHaveTextContent("default 20");
  });
});

describe("latency", () => {
  it("has its own tab with both checkers", async () => {
    overrides["/api/settings/latency"] = {
      paired: false, structural: [],
      pipelines: { telegram: { hops: [], recent: [] }, engine: { hops: [], recent: [] } },
    };
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Latency" }));

    expect(await screen.findByTestId("checker-telegram")).toBeInTheDocument();
    expect(screen.getByTestId("checker-engine")).toBeInTheDocument();
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
    overrides["/api/settings/diagnostics"] = {
      log: [],
      circuit_breaker: {
        enabled: true, losses_threshold: 3, cooldown_mins: 60,
        active_until: 0, consec_losses: 0, is_active: false, remaining_secs: 0,
      },
    };
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

  // The Updates section this tab used to carry was replaced on 2026-09-20 by
  // `GitHubUpdateSection`, which shows the installed commit against GitHub's
  // and applies the update through the route that actually applies one. Its
  // own tests moved with it, to `GitHubUpdateSection.test.tsx`; the AI summary
  // they also pinned now belongs to the header's popup
  // (`shell/__tests__/UpdateBadge.test.tsx`), which is the only caller that
  // pays for one. What is left here is the tab-level fact: the card is on
  // this tab and it reports the check.
  it("carries the GitHub update card", async () => {
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Node & updates" }));

    expect(await screen.findByTestId("update-status")).toHaveTextContent(/up to date/i);
  });

  it("says when autostart is not supported rather than offering a dead switch", async () => {
    overrides["/api/node/state"] = {
      ...(BODIES["/api/node/state"] as object),
      autostart: { supported: false, enabled: false, installed: false,
                   armed: false, check_interval_secs: 120 },
    };
    render(<SettingsPanel />);
    await userEvent.click(await screen.findByRole("tab", { name: "Node & updates" }));

    // Reworded 2026-09-21 with the section (KeepAliveSection): the same rule,
    // which is that an unsupported platform gets a sentence and no switch.
    expect(await screen.findByText(/Not supported on this platform/))
      .toBeInTheDocument();
    expect(screen.queryByLabelText(/Keep the app, the bridge and MT5 running/))
      .toBeNull();
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
    expect(screen.getByText("one is stored; leave blank to use it")).toBeInTheDocument();
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

  describe("how the other machine reaches this one", () => {
    // A fresh VPS said "listening" and nothing else (2026-09-25): no address
    // to dial, no word on the firewall, nothing on what protects the link.
    const REACH = {
      addresses: ["38.253.124.25"], behind_nat: false, firewall: "missing",
      firewall_command: 'netsh advfirewall firewall add rule name="FOREX Trader Sync (port 8765)"',
      security: "Encrypted with TLS; every connection must present the shared pairing token.",
    };
    // Shown on the VPS only (owner, 2026-09-25): on a main computer the port
    // is meant to stay closed, and "not open" there would read as a fault.
    const openWith = async (reachability: object, enabled = true) => {
      overrides["/api/remote/state"] = {
        ...REMOTE, server: { ...REMOTE.server, enabled, running: enabled, reachability },
      };
      render(<SettingsPanel />);
      await userEvent.click(await screen.findByRole("tab", { name: "Remote node" }));
    };

    it("is not shown on a machine that is not the VPS", async () => {
      await openWith(REACH, false);

      await screen.findByRole("button", { name: "Make this node a VPS" });
      expect(screen.queryByTestId("remote-reachability")).toBeNull();
    });

    it("names the address and port to enter on the other machine", async () => {
      await openWith(REACH);

      const section = await screen.findByTestId("remote-reachability");
      expect(section).toHaveTextContent("38.253.124.25");
      expect(section).toHaveTextContent("8765");
    });

    it("says the firewall rule is missing and gives the command", async () => {
      await openWith(REACH);

      const section = await screen.findByTestId("remote-reachability");
      expect(section).toHaveTextContent(/not open/i);
      expect(section).toHaveTextContent(REACH.firewall_command);
    });

    it("says so when the port is open", async () => {
      await openWith({ ...REACH, firewall: "open" });

      const section = await screen.findByTestId("remote-reachability");
      expect(section).toHaveTextContent(/open in the Windows firewall/i);
      expect(section).not.toHaveTextContent("netsh");
    });

    it("warns that a private address is not the one to dial", async () => {
      await openWith({ ...REACH, addresses: ["10.0.0.4"], behind_nat: true });

      expect(await screen.findByTestId("remote-reachability"))
        .toHaveTextContent(/public IP/i);
    });

    it("states what protects the link", async () => {
      await openWith(REACH);

      expect(await screen.findByTestId("remote-reachability"))
        .toHaveTextContent("pairing token");
    });
  });

  it("shows a refusal in the backend's own words", async () => {
    overrides["/api/remote/make-vps"] = {
      __status: 409,
      error: { kind: "refusal", message: "The sync server did not start: address in use", ref: null },
    };
    await openTab();

    await userEvent.click(
      await screen.findByRole("button", { name: "Make this node a VPS" }));

    expect(await screen.findByRole("alert"))
      .toHaveTextContent("The sync server did not start");
  });

  describe("making this node a VPS (owner, 2026-09-25)", () => {
    // The installer no longer opens the sync port: most Windows installs are
    // someone's main PC. One press here does what a VPS needs instead.

    it("offers it on a machine that is not the VPS, and says when not to", async () => {
      await openTab();

      expect(await screen.findByRole("button", { name: "Make this node a VPS" }))
        .toBeInTheDocument();
      expect(screen.getByTestId("remote-server")).toHaveTextContent(/main computer/i);
      expect(screen.queryByRole("button", { name: "Stop being a VPS" })).toBeNull();
    });

    it("sets it up in one press and says what happened", async () => {
      overrides["/api/remote/make-vps"] = {
        ...REMOTE, server: { ...REMOTE.server, enabled: true, running: true },
        note: "This machine is now the VPS. Port 8765 is open in the Windows firewall.",
      };
      await openTab();

      await userEvent.click(
        await screen.findByRole("button", { name: "Make this node a VPS" }));

      await waitFor(() => expect(writes()).toHaveLength(1));
      expect(writes()[0][0]).toBe("/api/remote/make-vps");
      expect(writes()[0][1].method).toBe("POST");
      expect(JSON.parse(writes()[0][1].body)).toEqual({ port: 8765 });
      expect(await screen.findByRole("status")).toHaveTextContent("now the VPS");
    });

    it("shows a newly made pairing token, because it is shown only once", async () => {
      overrides["/api/remote/make-vps"] = {
        ...REMOTE, token: "fresh-token-123", note: "This machine is now the VPS.",
      };
      await openTab();

      await userEvent.click(
        await screen.findByRole("button", { name: "Make this node a VPS" }));

      expect(await screen.findByTestId("remote-new-token"))
        .toHaveTextContent("fresh-token-123");
    });

    it("offers to stop being the VPS once it is one", async () => {
      overrides["/api/remote/state"] = {
        ...REMOTE, server: { ...REMOTE.server, enabled: true, running: true },
      };
      render(<SettingsPanel />);
      await userEvent.click(await screen.findByRole("tab", { name: "Remote node" }));

      await userEvent.click(
        await screen.findByRole("button", { name: "Stop being a VPS" }));

      await waitFor(() => expect(writes()).toHaveLength(1));
      expect(writes()[0][0]).toBe("/api/remote/stop-vps");
      expect(screen.queryByRole("button", { name: "Make this node a VPS" })).toBeNull();
    });

    // 2026-09-25: a VPS that kept its old token showed none, and the operator
    // pasted the certificate fingerprint into the other machine's token box.
    const openVps = async () => {
      overrides["/api/remote/state"] = {
        ...REMOTE, server: { ...REMOTE.server, enabled: true, running: true },
      };
      render(<SettingsPanel />);
      await userEvent.click(await screen.findByRole("tab", { name: "Remote node" }));
    };

    it("makes a new pairing token on the VPS and shows it", async () => {
      overrides["/api/node/sync-token"] = {
        token: "brand-new-token", note: "Copy this into the other node now.",
      };
      await openVps();

      await userEvent.click(
        await screen.findByRole("button", { name: "New pairing token" }));

      await waitFor(() => expect(writes()).toHaveLength(1));
      expect(writes()[0][0]).toBe("/api/node/sync-token");
      expect(await screen.findByTestId("remote-new-token"))
        .toHaveTextContent("brand-new-token");
    });

    it("says the fingerprint is not the token", async () => {
      await openVps();

      expect(await screen.findByText(/Cert fingerprint/))
        .toHaveTextContent(/not the pairing token/i);
    });

    it("offers to open the port when the VPS's firewall rule is missing", async () => {
      overrides["/api/remote/state"] = {
        ...REMOTE,
        server: {
          ...REMOTE.server, enabled: true, running: true,
          reachability: { addresses: ["38.253.124.25"], firewall: "missing",
            firewall_command: "netsh ...", security: "TLS" },
        },
      };
      render(<SettingsPanel />);
      await userEvent.click(await screen.findByRole("tab", { name: "Remote node" }));

      await userEvent.click(
        await screen.findByRole("button", { name: "Open port 8765" }));

      await waitFor(() => expect(writes()).toHaveLength(1));
      expect(writes()[0][0]).toBe("/api/remote/make-vps");
    });
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
