import { useState } from "react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { asArray, asObject } from "@/lib/asArray";
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
/**
 * The expiry line: the date, how long is left, and how alarmed to be.
 *
 * A bare date is a number the operator has to do arithmetic on. The day count
 * is what tells them whether this matters today.
 */
function describeExpiry(raw: string): { label: string; tone: string } {
  if (!raw || raw === "perpetual") return { label: "Never", tone: "text-profit" };
  const when = Date.parse(`${raw}T00:00:00Z`);
  if (Number.isNaN(when)) return { label: raw, tone: "text-ink-2" };
  const days = Math.round((when - Date.now()) / 86_400_000);
  if (days < 0) return { label: `${raw} — expired`, tone: "text-loss" };
  if (days <= 30) return { label: `${raw} — ${days} days`, tone: "text-warning" };
  return { label: `${raw} — ${days} days`, tone: "text-profit" };
}

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

  // ── Licence ──────────────────────────────────────────────────────────────
  const reg = asObject(node.data.registration);
  const registered = reg["connected"] === true;
  const lastError = String(reg["last_error"] ?? "");
  const licenceEmail = String(reg["email"] ?? "") || node.data.registered_email || "";
  // A blank subscription is a perpetual one, which is what the NiceGUI page
  // called it. Showing an empty cell instead reads as "unknown".
  const subscriptionType = String(reg["subscription_type"] ?? "") || "Perpetual";
  const expiry = describeExpiry(String(reg["subscription_expiry"] ?? ""));

  // `update` is the CHECK result, which is always an object -- the flag is
  // inside it. Treating the object's presence as "an update exists" meant
  // this screen claimed one on every install and left the Apply button
  // enabled, and Apply force-checks-out origin and discards local changes.
  // The test fixture said `update: null`, a shape the endpoint never
  // returns, so both tests agreed with the bug. Fixed 2026-09-20.
  const check = asObject(asObject(update.data)["update"]);
  const available = check["available"] === true;
  const commits = asArray<Record<string, unknown>>(check["commits"]);
  // The AI summary when there is one, the raw commit subjects when there is
  // not. A summary costs a paid model call and can fail for reasons that have
  // nothing to do with the release -- and an operator still needs to see what
  // they are about to install.
  const summary = asArray<string>(asObject(update.data)["changes"]);
  const lines = summary.length > 0
    ? summary
    : commits.map((c) => String(c["summary"] ?? "")).filter(Boolean);
  const checkError = String(check["error"] ?? "");

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
        {/* `connected`, not `approved`. The endpoint has never returned an
            `approved` key, so every install -- registered or not -- was told
            it had not been approved. The subscription, its expiry and the
            registered email were on the NiceGUI page and were never ported. */}
        {registered ? (
          <>
            <p className="mt-0.5 text-[11px] text-profit">Authorised and registered.</p>
            <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-[11px]">
              <dt className="text-ink-3">Subscription</dt>
              <dd data-testid="licence-subscription" className="text-ink-1">
                {subscriptionType}
              </dd>
              <dt className="text-ink-3">Expires</dt>
              <dd data-testid="licence-expiry" className={`num ${expiry.tone}`}>
                {expiry.label}
              </dd>
              {licenceEmail && (
                <>
                  <dt className="text-ink-3">Email</dt>
                  <dd data-testid="licence-email" className="num text-ink-2">
                    {licenceEmail}
                  </dd>
                </>
              )}
            </dl>
          </>
        ) : (
          <>
            <p className="mt-0.5 text-[11px] text-ink-3">
              This machine has not been approved yet.
            </p>
            {lastError && (
              <p className="mt-0.5 text-[11px] text-warning">Last error: {lastError}</p>
            )}
          </>
        )}
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
            ? ` — ${commits.length} newer commit${commits.length === 1 ? "" : "s"}`
              + ` available (${String(check["remote_sha"] ?? "").slice(0, 7)}).`
            : update.data
              ? " — up to date."
              : ""}
        </p>
        {checkError && (
          <p className="mt-0.5 text-[11px] text-warning">{checkError}</p>
        )}
        {available && lines.length > 0 && (
          <ul className="mt-1.5 list-disc space-y-0.5 pl-4 text-[11px] text-ink-2">
            {lines.map((line, i) => <li key={i}>{line}</li>)}
          </ul>
        )}
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
