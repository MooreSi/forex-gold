import { RotateCcw } from "lucide-react";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { api } from "@/api/client";
import { asArray } from "@/lib/asArray";
import { useSettingsResource } from "../hooks/useSettingsResource";
import { SettingsField } from "../internal/SettingsField";

/** One row of the catalogue, exactly as the endpoint describes it. */
interface Tunable {
  key: string;
  label: string;
  value: number;
  default: number;
  min: number;
  max: number;
  unit: string;
  desc: string;
}

/**
 * Expert Tunables, rendered generically from the catalogue.
 *
 * Never hand-written per parameter: `/add-tunable` exists so a new tunable
 * appears here with no UI change at all, and a bespoke form per parameter is
 * exactly what that skill was written to avoid.
 *
 * The catalogue is `{ "<group name>": [ Tunable, ... ] }`. This tab used to
 * read it as `{ "<key>": {value, default} }`, so it rendered one empty box per
 * GROUP — four of them, labelled "Risk filters", "Instant Market Entry",
 * "Signal handling", "Broker reconciliation" — and every real tunable was
 * invisible and uneditable. The test fixture asserted the same invented shape,
 * which is why it went unnoticed until the screen was opened on 2026-09-20.
 *
 * Each row shows what the number means, what it defaults to and what it is
 * allowed to be. A tunable with no explanation is one nobody dares change,
 * which makes the screen decorative.
 */
export function TunablesTab() {
  const params = useSettingsResource<Record<string, Tunable[]>>(
    "/api/settings/expert-params",
  );

  if (!params.data) {
    return (
      <EmptyState
        title={params.error ? "Could not load the tunables" : "Loading"}
        hint={params.error ?? undefined}
      />
    );
  }

  const groups = Object.entries(params.data)
    .map(([name, rows]) => [name, asArray<Tunable>(rows)] as const)
    .filter(([, rows]) => rows.length > 0);

  if (groups.length === 0) {
    return <EmptyState title="No tunables are registered" />;
  }

  // Reset goes to its own endpoint. `params.save(body, "POST")` posts to the
  // catalogue path, which has no POST handler and answered 405 on every click
  // since the tab was written.
  const reset = async (key?: string) => {
    await api.post("/api/settings/expert-params/reset", key ? { key } : {});
    await params.reload();
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <p className="text-[11px] text-ink-3">
          Every registered tunable, straight from the catalogue. A new one appears
          here on its own.
        </p>
        <Button variant="ghost" onClick={() => void reset()} disabled={params.saving}>
          Reset all to defaults
        </Button>
      </div>

      {groups.map(([name, rows]) => (
        <section key={name} className="rounded-lg border border-line bg-surface-1 p-3">
          <h3 className="mb-2 text-xs font-semibold text-accent">{name}</h3>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {rows.map((t) => {
              const modified = t.value !== t.default;
              return (
                <div
                  key={t.key}
                  data-testid={`tunable-${t.key}`}
                  data-modified={modified ? "true" : "false"}
                  className="space-y-0.5"
                >
                  <div className="flex items-start gap-1">
                    <div className="min-w-0 flex-1">
                      <SettingsField
                        label={t.label}
                        type="number"
                        version={params.version}
                        value={String(t.value ?? "")}
                        onCommit={(v) =>
                          void params.save({ values: { [t.key]: Number(v) } })}
                      />
                    </div>
                    {modified && (
                      <button
                        type="button"
                        data-testid={`tunable-reset-${t.key}`}
                        title={`Reset ${t.label} to ${t.default}`}
                        aria-label={`Reset ${t.label}`}
                        onClick={() => void reset(t.key)}
                        className="mt-5 rounded p-1 text-ink-3 hover:text-ink-1"
                      >
                        <RotateCcw size={12} />
                      </button>
                    )}
                  </div>
                  <p
                    data-testid={`tunable-hint-${t.key}`}
                    className="text-[10px] text-ink-3"
                  >
                    default {String(t.default)}
                    {t.unit ? ` ${t.unit}` : ""} · allowed {String(t.min)}–{String(t.max)}
                  </p>
                  {t.desc && (
                    <p className="text-[10px] leading-snug text-ink-3">{t.desc}</p>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      ))}

      {params.error && <p role="alert" className="text-xs text-loss">{params.error}</p>}
    </div>
  );
}
