import { EmptyState } from "@/components/shared/EmptyState";
import { RISK_GROUPS, type RiskField } from "../content/risk";
import { useSettingsResource } from "../hooks/useSettingsResource";
import { SettingsField } from "../internal/SettingsField";
import { SettingsToggle } from "../internal/SettingsToggle";

/**
 * The numbers that decide how much money a trade can lose.
 *
 * Every field shows what was STORED after a save, not what was typed: the risk
 * service clamps, and a field that kept the operator's number would be telling
 * them the engine is using a value it is not.
 *
 * Restored to its full set on 2026-09-18. The first React version offered four
 * fields out of the original's twenty-two — and two of those four named columns
 * that do not exist, so they raised on save and the value was silently lost.
 * The list lives in `content/risk.ts` and is checked against the schema by
 * `tests/api/test_settings_writes_reach_the_store.py`.
 *
 * A setting whose switch is off says so beside it rather than quietly doing
 * nothing: "I turned it on and it made no difference" is how a setting gets
 * reported as broken.
 */
export function RiskTab() {
  const risk = useSettingsResource<Record<string, unknown>>("/api/settings/risk");
  const data = risk.data;

  if (!data) {
    return (
      <EmptyState
        title={risk.error ? "Could not load the risk settings" : "Loading"}
        hint={risk.error ?? undefined}
      />
    );
  }

  const on = (key: string) => {
    const raw = data[key];
    // `internal_hedge_mode` is a choice, not a flag: anything other than "off"
    // means the dependent field applies.
    return typeof raw === "string" ? raw !== "off" && raw !== "" : Boolean(Number(raw ?? 0));
  };

  const field = (f: RiskField) => {
    const dependencyOff = f.dependsOn && !on(f.dependsOn.key);

    if (f.kind === "toggle") {
      return (
        <div key={f.key} data-testid={`risk-${f.key}`}>
          <SettingsToggle
            label={f.label}
            hint={f.hint}
            checked={Boolean(Number(data[f.key] ?? 0))}
            onChange={(v) => void risk.save({ [f.key]: v ? 1 : 0 })}
          />
        </div>
      );
    }

    if (f.kind === "choice") {
      return (
        <label key={f.key} data-testid={`risk-${f.key}`} className="block text-xs text-ink-2">
          {f.label}
          <select
            aria-label={f.label}
            value={String(data[f.key] ?? "off")}
            onChange={(e) => void risk.save({ [f.key]: e.target.value })}
            className="mt-0.5 w-full rounded border border-line bg-surface-1 px-2 py-1 text-ink-1"
          >
            {f.choices?.map((c) => (
              <option key={c.value} value={c.value}>{c.label}</option>
            ))}
          </select>
          {f.hint && <span className="mt-0.5 block text-[10px] text-ink-3">{f.hint}</span>}
        </label>
      );
    }

    return (
      <div key={f.key} data-testid={`risk-${f.key}`}>
        <SettingsField
          label={f.label}
          hint={f.hint}
          type="number"
          version={risk.version}
          value={String(data[f.key] ?? "")}
          onCommit={(v) => void risk.save({ [f.key]: Number(v) })}
        />
        {dependencyOff && (
          <span className="mt-0.5 block text-[10px] text-warning">
            Does nothing until {f.dependsOn?.label} is on.
          </span>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-4">
      <p className="rounded border border-warning/40 bg-warning/10 px-3 py-2 text-[11px] text-warning">
        These decide how much a single trade can lose and when the account stops
        for the day. Every value is re-read from the backend after saving, so
        what you see is what the engine uses.
      </p>

      {RISK_GROUPS.map((group) => (
        <section key={group.title} data-testid={`risk-group-${group.title}`}
          className="rounded border border-line p-3">
          <h3 className="text-xs font-semibold text-ink-1">{group.title}</h3>
          {group.blurb && (
            <p className="mb-2 text-[11px] text-ink-3">{group.blurb}</p>
          )}
          <div className="grid items-start gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {group.fields.map(field)}
          </div>
        </section>
      ))}

      {risk.error && <p role="alert" className="text-xs text-loss">{risk.error}</p>}
    </div>
  );
}
