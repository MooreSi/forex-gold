import { EmptyState } from "@/components/shared/EmptyState";
import { Button } from "@/components/shared/Button";
import { useSettingsResource } from "../hooks/useSettingsResource";
import { SettingsField } from "../internal/SettingsField";

/**
 * Expert Tunables, rendered generically from the catalogue.
 *
 * Never hand-written per parameter: `/add-tunable` exists so a new tunable
 * appears here with no UI change at all, and a bespoke form per parameter is
 * exactly what that skill was written to avoid.
 */
export function TunablesTab() {
  const params = useSettingsResource<Record<string, Record<string, unknown>>>(
    "/api/settings/expert-params",
  );

  if (!params.data) {
    return <EmptyState title={params.error ? "Could not load the tunables" : "Loading"} hint={params.error ?? undefined} />;
  }

  const entries = Object.entries(params.data);
  if (entries.length === 0) {
    return <EmptyState title="No tunables are registered" />;
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-[11px] text-ink-3">
          Every registered tunable, straight from the catalogue. A new one appears
          here on its own.
        </p>
        <Button
          variant="ghost"
          onClick={() => void params.save({}, "POST")}
          disabled={params.saving}
        >
          Reset all to defaults
        </Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {entries.map(([key, meta]) => (
          <div key={key} data-testid={`tunable-${key}`}>
            <SettingsField
              label={key}
              type="number"
              version={params.version}
              hint={
                meta["default"] !== undefined
                  ? `default ${String(meta["default"])}`
                  : undefined
              }
              value={String(meta["value"] ?? "")}
              onCommit={(v) => void params.save({ values: { [key]: Number(v) } })}
            />
          </div>
        ))}
      </div>
      {params.error && <p role="alert" className="text-xs text-loss">{params.error}</p>}
    </div>
  );
}
