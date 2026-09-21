import { EmptyState } from "@/components/shared/EmptyState";
import { useSettingsResource } from "../hooks/useSettingsResource";
import { Tooltip } from "@/components/shared/Tooltip";

interface AccessState {
  auto_login: boolean;
  /** Written by the backend, not composed here. See below. */
  warning: string;
}

interface LicenceState {
  email: string;
  licence_type: string;
  expiry_date: string;
  machine_id: string;
  key_masked: string;
}

/**
 * Who can open this app on this machine, and who it is licensed to.
 *
 * Restored 2026-09-18 — both halves were their own NiceGUI tabs (Security and
 * Registration) and the React port dropped both, so there was no way to see
 * the licence or to change whether a restart asks for the password.
 *
 * They are one tab here because they answer the same question from two sides:
 * who is allowed in, and who this install belongs to. The access control is
 * first because it is the one that can be got wrong.
 *
 * **The warning text comes from the backend.** It is the one thing the
 * operator has to weigh before switching the prompt off, and a UI that forgot
 * to render it would be offering the choice without the consequence.
 */
export function AccessTab() {
  const access = useSettingsResource<AccessState>("/api/settings/access");
  const licence = useSettingsResource<LicenceState>("/api/node/licence");

  if (!access.data) {
    return (
      <EmptyState
        title={access.error ? "Could not load the access settings" : "Loading"}
        hint={access.error ?? undefined}
      />
    );
  }

  const auto = access.data.auto_login === true;

  return (
    <div className="space-y-4">
      <section data-testid="app-access" className="rounded border border-line p-3">
        <h3 className="text-xs font-semibold text-ink-1">App access</h3>
        <p className="mb-2 text-[11px] text-ink-3">What happens when the app restarts.</p>

        <div className="space-y-1.5">
          {[
            { value: false, label: "Ask for the dashboard password",
              help: "Anybody opening the dashboard has to sign in. The right choice on a machine other people can reach." },
            { value: true, label: "Log in automatically",
              help: "The dashboard opens straight into the app with no password — including after a restart, and for anyone who can reach this machine's browser. It does not disable the licence or change what the engines do." },
          ].map((opt) => (
            <label key={String(opt.value)} className="flex items-center gap-2 text-xs text-ink-2">
              <Tooltip label={opt.help}>
                <input
                  type="radio"
                  name="app-access"
                  aria-label={opt.label}
                  checked={auto === opt.value}
                  onChange={() => void access.save({ auto_login: opt.value })}
                  className="accent-accent"
                />
              </Tooltip>
              {opt.label}
            </label>
          ))}
        </div>

        {access.data.warning && (
          <p role="alert" className="mt-2 rounded border border-warning/40 bg-warning/10 px-2 py-1.5 text-[11px] text-warning">
            {access.data.warning}
          </p>
        )}
        {access.error && <p role="alert" className="mt-1 text-xs text-loss">{access.error}</p>}
      </section>

      <section data-testid="registration" className="rounded border border-line p-3">
        <h3 className="text-xs font-semibold text-ink-1">Registration</h3>
        <p className="mb-2 text-[11px] text-ink-3">
          Who this install is licensed to. Read-only — the licence itself is
          checked before the app starts, not here.
        </p>

        {licence.data ? (
          <dl className="grid gap-x-6 gap-y-1 text-[11px] sm:grid-cols-2">
            {[
              ["Email", licence.data.email],
              ["Licence type", licence.data.licence_type],
              ["Expires", licence.data.expiry_date],
              ["Machine ID", licence.data.machine_id],
              // Masked in the backend, not here: a credential never leaves the
              // machine whole, and masking in the browser means it already has.
              ["Registration key", licence.data.key_masked],
            ].map(([label, value]) => (
              <div key={label} className="flex gap-2">
                <dt className="text-ink-3">{label}</dt>
                <dd className="num break-all text-ink-2">{value || "—"}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="text-[11px] text-ink-3">
            {licence.error ?? "Loading"}
          </p>
        )}
      </section>
    </div>
  );
}
