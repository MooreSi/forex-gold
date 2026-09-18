import { EmptyState } from "@/components/shared/EmptyState";
import { Button } from "@/components/shared/Button";
import { asArray, asObject } from "@/lib/asArray";
import { useSettingsResource } from "../hooks/useSettingsResource";

interface Diagnostics {
  log: string[][];
  circuit_breaker: Record<string, unknown>;
}

/** The log since this app started, and the circuit breaker. */
export function DiagnosticsTab() {
  const diag = useSettingsResource<Diagnostics>("/api/settings/diagnostics");
  const reset = useSettingsResource<Record<string, unknown>>(
    "/api/settings/circuit-breaker/reset",
  );

  if (!diag.data) {
    return <EmptyState title={diag.error ? "Could not load diagnostics" : "Loading"} hint={diag.error ?? undefined} />;
  }

  const breaker = asObject(diag.data.circuit_breaker);
  const log = asArray<string[]>(diag.data.log);
  const tripped = breaker["tripped"] === true;

  return (
    <div className="space-y-3">
      <div
        data-testid="circuit-breaker"
        data-tripped={tripped}
        className="flex items-center gap-3 rounded border border-line bg-surface-2 px-3 py-2"
      >
        <span className={tripped ? "text-xs text-loss" : "text-xs text-profit"}>
          Circuit breaker {tripped ? "tripped" : "clear"}
        </span>
        {typeof breaker["reason"] === "string" && breaker["reason"] && (
          <span className="text-[11px] text-ink-3">{breaker["reason"]}</span>
        )}
        <Button
          className="ml-auto"
          variant="ghost"
          disabled={reset.saving}
          disabledReason={tripped ? null : "The breaker is not tripped."}
          onClick={async () => {
            await reset.save({}, "POST");
            await diag.reload();
          }}
        >
          Reset
        </Button>
      </div>

      <div>
        <h3 className="mb-1 text-xs font-semibold text-ink-1">Log since this app started</h3>
        {log.length === 0 ? (
          <p className="text-[11px] text-ink-3">Nothing worth reporting yet.</p>
        ) : (
          <ul className="num max-h-80 space-y-0.5 overflow-auto rounded border border-line bg-surface-1 p-2 text-[11px]">
            {log.map((line, i) => (
              <li key={i} className="flex gap-2">
                <span className="shrink-0 text-ink-3">{line[0]}</span>
                <span className="text-ink-2">{line[1]}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
