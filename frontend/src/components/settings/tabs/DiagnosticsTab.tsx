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

  // `is_active`, not `tripped`. The endpoint has never returned a `tripped`
  // key, so this card said "clear" whatever the breaker was doing -- on the
  // one screen whose job is to say whether orders are being blocked -- and
  // its Reset button was permanently disabled, in exactly the state it
  // exists for. The test fixture invented `{tripped, reason}` to match.
  const enabled = breaker["enabled"] === true;
  const tripped = breaker["is_active"] === true;
  const state = tripped ? "tripped" : enabled ? "clear" : "off";

  // Built from the numbers the endpoint gives rather than expecting the
  // backend to compose a sentence. `remaining_secs` matters: this breaker
  // clears itself, and "tripped" with no horizon reads as permanent.
  const losses = Number(breaker["consec_losses"] ?? 0);
  const threshold = Number(breaker["losses_threshold"] ?? 0);
  const remaining = Number(breaker["remaining_secs"] ?? 0);
  const why = tripped
    ? [
      losses > 0 ? `${losses} consecutive losses` : `${threshold} consecutive losses`,
      remaining > 0 ? `clears in ${Math.ceil(remaining / 60)}m` : "",
    ].filter(Boolean).join(" — ")
    : enabled
      ? `after ${threshold} consecutive losses, for ${Number(breaker["cooldown_mins"] ?? 0)}m`
      : "Switched off in Trading > Risk — nothing is guarding against a losing run.";

  return (
    <div className="space-y-3">
      <div
        data-testid="circuit-breaker"
        data-tripped={tripped}
        className="flex items-center gap-3 rounded border border-line bg-surface-2 px-3 py-2"
      >
        <span className={
          tripped ? "text-xs text-loss" : enabled ? "text-xs text-profit" : "text-xs text-ink-3"
        }>
          Circuit breaker {state}
        </span>
        {why && <span className="text-[11px] text-ink-3">{why}</span>}
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
