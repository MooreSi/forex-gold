import { useState } from "react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { asObject } from "@/lib/asArray";
import { useSettingsResource } from "../hooks/useSettingsResource";
import { SettingsField } from "../internal/SettingsField";

interface NodeState {
  version: string;
  active_trader: string;
  sync_token_set: boolean;
  registration: Record<string, unknown>;
  registered_email: string;
  autostart: Record<string, unknown>;
}

/**
 * Pairing, autostart, restart and updates.
 *
 * The sync token is shown exactly once, when it is generated. It is never read
 * back, so this screen can only say whether one exists — which is the honest
 * thing it knows.
 */
export function NodeTab() {
  const node = useSettingsResource<NodeState>("/api/node/state");
  const token = useSettingsResource<{ token: string; note: string }>("/api/node/sync-token");
  const update = useSettingsResource<Record<string, unknown>>("/api/node/update");
  const [email, setEmail] = useState("");
  const [nickname, setNickname] = useState("");

  if (!node.data) {
    return <EmptyState title={node.error ? "Could not load the node settings" : "Loading"} hint={node.error ?? undefined} />;
  }

  const autostart = asObject(node.data.autostart);
  const available = asObject(update.data)["update"];

  return (
    <div className="space-y-5">
      <section>
        <h3 className="text-xs font-semibold text-ink-1">Pairing</h3>
        <p className="mt-0.5 text-[11px] text-ink-3">
          {node.data.sync_token_set
            ? "A sync token is stored. It is never shown again after it is created."
            : "No sync token yet — this node is not paired."}
        </p>
        <div className="mt-2 flex items-center gap-2">
          <Button
            onClick={async () => {
              await token.save({}, "POST");
              await node.reload();
            }}
            disabled={token.saving}
          >
            Generate a new token
          </Button>
          <span className="text-[11px] text-ink-2">
            Active trader: <span className="num">{node.data.active_trader}</span>
          </span>
        </div>
        {token.data?.token && (
          <div
            data-testid="new-sync-token"
            className="mt-2 rounded border border-warning/40 bg-warning/10 px-3 py-2"
          >
            <p className="num text-sm text-ink-1">{token.data.token}</p>
            <p className="mt-1 text-[11px] text-warning">{token.data.note}</p>
          </div>
        )}
      </section>

      <section>
        <h3 className="text-xs font-semibold text-ink-1">Licence registration</h3>
        <p className="mt-0.5 text-[11px] text-ink-3">
          {node.data.registration["approved"] === true
            ? `Approved${node.data.registered_email ? ` for ${node.data.registered_email}` : ""}.`
            : "This machine has not been approved yet."}
        </p>
        <div className="mt-2 grid gap-3 sm:grid-cols-3">
          <SettingsField label="Email" value={email} onCommit={setEmail} />
          <SettingsField label="Nickname" value={nickname} onCommit={setNickname} />
          <div className="flex items-end">
            <Button
              onClick={() => void node.save({ email, nickname }, "POST")}
              disabledReason={email.trim() ? null : "An email address is needed to register."}
            >
              Request approval
            </Button>
          </div>
        </div>
      </section>

      <section>
        <h3 className="text-xs font-semibold text-ink-1">Start with the machine</h3>
        {autostart["supported"] === true ? (
          <label className="mt-1 flex items-center gap-2 text-xs text-ink-2">
            <input
              type="checkbox"
              aria-label="Start the app when this machine boots"
              checked={autostart["installed"] === true}
              onChange={async (e) => {
                await node.save({ enabled: e.target.checked });
                await node.reload();
              }}
              className="accent-accent"
            />
            Start the app when this machine boots
          </label>
        ) : (
          <p className="mt-0.5 text-[11px] text-ink-3">
            Not supported on this platform.
          </p>
        )}
      </section>

      <section>
        <h3 className="text-xs font-semibold text-ink-1">Updates</h3>
        <p className="mt-0.5 text-[11px] text-ink-3">
          Running <span className="num">{node.data.version}</span>
          {available
            ? ` — ${String(asObject(available)["version"] ?? "a newer release")} is available.`
            : update.data
              ? " — up to date."
              : ""}
        </p>
        <div className="mt-2 flex items-center gap-2">
          <Button variant="ghost" onClick={() => void update.reload()}>
            Check for updates
          </Button>
          <Button
            onClick={() => void update.save({}, "POST")}
            disabledReason={available ? null : "There is no update to apply."}
          >
            Apply the update
          </Button>
        </div>
      </section>
    </div>
  );
}
