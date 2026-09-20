import { useState } from "react";
import { api } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { asObject } from "@/lib/asArray";
import { useSettingsResource } from "../hooks/useSettingsResource";
import { GitHubUpdateSection } from "../internal/GitHubUpdateSection";
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
  // NOT `useSettingsResource`. That hook GETs its path on mount, and
  // `/api/node/sync-token` is POST-only by design — reading a stored token
  // back is precisely what it refuses to do — so every visit to this tab
  // fired a request that answered 405. The token exists for exactly one
  // moment, which is the response to the POST, so it is held here.
  const [token, setToken] = useState<{ token: string; note: string } | null>(null);
  const [generating, setGenerating] = useState(false);
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
              setGenerating(true);
              try {
                setToken(await api.post<{ token: string; note: string }>(
                  "/api/node/sync-token", {},
                ));
                await node.reload();
              } finally {
                setGenerating(false);
              }
            }}
            disabled={generating}
          >
            Generate a new token
          </Button>
          <span className="text-[11px] text-ink-2">
            Active trader: <span className="num">{node.data.active_trader}</span>
          </span>
        </div>
        {token?.token && (
          <div
            data-testid="new-sync-token"
            className="mt-2 rounded border border-warning/40 bg-warning/10 px-3 py-2"
          >
            <p className="num text-sm text-ink-1">{token.token}</p>
            <p className="mt-1 text-[11px] text-warning">{token.note}</p>
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

      <GitHubUpdateSection version={node.data.version} />
    </div>
  );
}
