import { useState } from "react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { useSettingsResource } from "../hooks/useSettingsResource";
import { SettingsField } from "../internal/SettingsField";
import { SettingsToggle } from "../internal/SettingsToggle";

interface Account { prefix: string; title: string; blurb: string }

/**
 * Which MT5 accounts the bridge can authenticate as, and how it runs.
 *
 * **Both accounts, not one.** The tab offered only the demo credentials until
 * 2026-09-18, so the live account could never be configured — which also meant
 * the demo/live switch could never be used, because it refuses to switch to an
 * account it has no credentials for.
 *
 * Both live in one row of the master database, deliberately: they have to be
 * readable while the app is pointed at either environment, and a
 * per-environment copy is one that goes stale on whichever side was not
 * edited. The field names differ rather than the table.
 *
 * The password is write-only. The backend never sends one and this form never
 * shows one; it reports whether a password is stored, which is a different
 * question and the only one a UI needs to answer.
 */
const ACCOUNTS: Account[] = [
  {
    prefix: "", title: "Demo account",
    blurb: "The account this app uses unless it is switched to live.",
  },
  {
    prefix: "live_", title: "Live account",
    blurb: "Real money. Nothing uses these until the app is switched to live from the header.",
  },
];

export function Mt5Tab() {
  const mt5 = useSettingsResource<Record<string, unknown>>("/api/settings/mt5");
  const risk = useSettingsResource<Record<string, unknown>>("/api/settings/risk");
  const app = useSettingsResource<Record<string, unknown>>("/api/settings/app");

  if (!mt5.data) {
    return (
      <EmptyState
        title={mt5.error ? "Could not load the MT5 settings" : "Loading"}
        hint={mt5.error ?? undefined}
      />
    );
  }

  return (
    <div className="space-y-4">
      {ACCOUNTS.map((account) => (
        <AccountSection key={account.prefix} account={account}
          stored={mt5.data as Record<string, unknown>}
          onSave={(body) => mt5.save(body)} />
      ))}

      <section data-testid="ea-bridge" className="rounded border border-line p-3">
        <h3 className="text-xs font-semibold text-ink-1">EA Bridge</h3>
        <p className="mb-2 text-[11px] text-ink-3">
          Hands trade management to an Expert Advisor running inside MetaTrader,
          so stops and partials are applied at the terminal rather than over the
          wire. If the EA disconnects or stops responding, any trade it was
          managing is reclaimed by the app — never left unmanaged. Off means
          every trade is managed by the app exactly as before.
        </p>
        {risk.data && (
          <SettingsToggle
            label="Use the EA bridge"
            checked={Boolean(Number(risk.data["ea_bridge_enabled"] ?? 0))}
            onChange={(v) => void risk.save({ ea_bridge_enabled: v ? 1 : 0 })}
          />
        )}
      </section>

      <section data-testid="bridge-process" className="rounded border border-line p-3">
        <h3 className="text-xs font-semibold text-ink-1">How the bridge runs</h3>
        <p className="mb-2 text-[11px] text-ink-3">
          MetaTrader is a Windows program, so on macOS it runs under CrossOver
          or an independent Wine prefix. Changing these takes effect the next
          time the bridge starts.
        </p>
        {app.data && (
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block text-xs text-ink-2">
              Backend
              <select
                aria-label="Backend"
                value={String(app.data["bridge_backend"] ?? "crossover")}
                onChange={(e) => void app.save({ bridge_backend: e.target.value })}
                className="mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
              >
                <option value="crossover">CrossOver</option>
                <option value="wine">Wine (independent prefix)</option>
              </select>
              <span className="mt-0.5 block text-[10px] text-ink-3">
                Wine uses the prefix at the bottle path below; run
                setup_wine_bridge.sh first.
              </span>
            </label>
            <SettingsField
              label="Bridge URL"
              hint="Where the app reaches the bridge. Leave it alone unless you moved it."
              version={app.version}
              value={String(app.data["mt5_bridge_url"] ?? "")}
              onCommit={(v) => void app.save({ mt5_bridge_url: v })}
            />
          </div>
        )}
      </section>
    </div>
  );
}

function AccountSection(
  { account, stored, onSave }: {
    account: Account;
    stored: Record<string, unknown>;
    onSave: (body: unknown) => Promise<void>;
  },
) {
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [server, setServer] = useState("");

  const live = account.prefix === "live_";
  const storedLogin = String(stored[`${account.prefix}login`] ?? "");
  const storedServer = String(stored[`${account.prefix}server`] ?? "");
  // Redaction turns `live_password_enc` into `live_password_enc_set`.
  const passwordStored = stored[`${account.prefix}password_enc_set`] === true;

  const submit = async () => {
    await onSave({
      login, password, server,
      environment: live ? "live" : "demo",
    });
    setPassword("");
  };

  return (
    <section data-testid={`mt5-${live ? "live" : "demo"}`}
      className={`rounded border p-3 ${live ? "border-loss/40" : "border-line"}`}>
      <h3 className={`text-xs font-semibold ${live ? "text-loss" : "text-ink-1"}`}>
        {account.title}
      </h3>
      <p className="mb-2 text-[11px] text-ink-3">{account.blurb}</p>

      <div className="mb-2 rounded border border-line bg-surface-2 px-3 py-2 text-xs text-ink-2">
        <p>
          Configured:{" "}
          <span className="num text-ink-1">{storedLogin || "none"}</span>
          {storedServer ? ` on ${storedServer}` : ""}
        </p>
        <p className="mt-0.5 text-[11px] text-ink-3">
          {passwordStored
            ? "A password is stored. It is never sent to this screen."
            : "No password is stored, so the bridge cannot log in to this account."}
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <SettingsField label={`${account.title} login`} value={login} onCommit={setLogin} />
        <SettingsField label={`${account.title} password`} value={password}
          type="password" onCommit={setPassword} />
        <SettingsField label={`${account.title} server`} value={server} onCommit={setServer} />
      </div>

      <div className="mt-2">
        <Button
          onClick={() => void submit()}
          disabledReason={
            login && password && server
              ? undefined
              : "A login, password and server are all needed."
          }
        >
          Save {live ? "live" : "demo"} credentials
        </Button>
      </div>
    </section>
  );
}
