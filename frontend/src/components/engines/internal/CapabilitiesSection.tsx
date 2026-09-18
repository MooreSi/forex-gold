import { CAPABILITIES } from "../content/capabilities";

interface CapabilitiesSectionProps {
  settings: Record<string, unknown>;
  onSave: (key: string, value: number) => void;
}

function on(settings: Record<string, unknown>, key: string): boolean {
  return Boolean(Number(settings[key] ?? 0));
}

/**
 * The Reversal engine's capability switches.
 *
 * A switch whose dependency is off renders that fact beside it rather than
 * quietly doing nothing. "Does NOTHING unless X is also on" was a tooltip in
 * the NiceGUI panel; a tooltip is not where you put the reason a setting has
 * no effect.
 */
export function CapabilitiesSection({ settings, onSave }: CapabilitiesSectionProps) {
  return (
    <div className="grid gap-2 md:grid-cols-2">
      {CAPABILITIES.map((cap) => {
        const dependencyOff = cap.dependsOn && !on(settings, cap.dependsOn.key);
        return (
          <label
            key={cap.key}
            data-testid={`capability-${cap.key}`}
            className="flex h-full flex-col rounded border border-line bg-surface-2 p-2.5"
          >
            <span className="flex items-start gap-2">
              <input
                type="checkbox"
                aria-label={cap.label}
                checked={on(settings, cap.key)}
                onChange={(e) => onSave(cap.key, e.target.checked ? 1 : 0)}
                className="mt-0.5 accent-accent"
              />
              <span className="text-xs font-semibold text-ink-1">{cap.label}</span>
            </span>
            <span className="mt-1 text-[11px] leading-snug text-ink-3">
              {cap.description}
            </span>
            {cap.dependsOn && (
              <span
                className={
                  dependencyOff
                    ? "mt-1 text-[11px] text-warning"
                    : "mt-1 text-[11px] text-ink-3"
                }
              >
                Does nothing unless “{cap.dependsOn.label}” is also on
                ({cap.dependsOn.where})
                {dependencyOff ? " — it is currently off." : "."}
              </span>
            )}
          </label>
        );
      })}
    </div>
  );
}
