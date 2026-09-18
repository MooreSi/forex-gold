import { useState } from "react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { useSettingsResource } from "../hooks/useSettingsResource";
import { SettingsField } from "../internal/SettingsField";

/**
 * Which MT5 account the bridge authenticates as.
 *
 * The password is write-only: the backend never sends it, and this form never
 * shows one. It reports whether a password is stored, which is a different
 * question and the only one a UI needs to answer.
 */
export function Mt5Tab() {
  const mt5 = useSettingsResource<Record<string, unknown>>("/api/settings/mt5");
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [server, setServer] = useState("");

  if (!mt5.data) {
    return <EmptyState title={mt5.error ? "Could not load the MT5 settings" : "Loading"} hint={mt5.error ?? undefined} />;
  }

  const stored = mt5.data;
  const submit = async () => {
    await mt5.save({ login, password, server });
    setPassword("");
  };

  return (
    <div className="space-y-3">
      <div className="rounded border border-line bg-surface-2 px-3 py-2 text-xs text-ink-2">
        <p>
          Configured account:{" "}
          <span className="num text-ink-1">{String(stored["login"] ?? "none")}</span>
          {stored["server"] ? ` on ${String(stored["server"])}` : ""}
        </p>
        <p className="mt-0.5 text-[11px] text-ink-3">
          {stored["password_set"] === true
            ? "A password is stored. It is never sent to this screen."
            : "No password is stored, so the bridge cannot log in."}
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <SettingsField label="Login" value={login} onCommit={setLogin} />
        <SettingsField label="Password" value={password} type="password" onCommit={setPassword} />
        <SettingsField label="Server" value={server} onCommit={setServer} />
      </div>

      <Button
        onClick={() => void submit()}
        disabled={mt5.saving}
        disabledReason={
          login && password && server ? null : "Enter the login, password and server."
        }
      >
        {mt5.saving ? "Saving…" : "Save and sync to the bridge"}
      </Button>
      <p className="text-[11px] text-ink-3">
        Saving writes the credentials and pushes them to the bridge's own file in
        one step. Doing only the first leaves the bridge logged in as the
        previous account.
      </p>
      {mt5.error && <p role="alert" className="text-xs text-loss">{mt5.error}</p>}
    </div>
  );
}
