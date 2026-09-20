import { useState } from "react";
import { Download } from "lucide-react";
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
  const [exporting, setExporting] = useState(false);
  const [exportNote, setExportNote] = useState("");

  /**
   * The filtered logs, saved to disk.
   *
   * The NiceGUI app mailed this to a hardcoded address; a browser can simply
   * hand the operator the file. Fetched rather than linked so the failure is
   * visible here instead of as a blank tab, and so the counts in the response
   * headers can be reported -- a bundle of 40 lines out of 200,000 scanned is
   * a normal, quiet machine, and saying so stops it reading as a failure.
   */
  async function downloadBundle() {
    setExporting(true);
    setExportNote("");
    try {
      const res = await fetch("/api/settings/log-bundle?days=5");
      if (!res.ok) throw new Error(`the server answered ${res.status}`);
      const blob = await res.blob();
      const name = /filename="([^"]+)"/.exec(
        res.headers.get("content-disposition") ?? "")?.[1]
        ?? "forex_trader_logs.txt";
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.click();
      URL.revokeObjectURL(url);
      const kept = res.headers.get("x-log-lines-kept") ?? "?";
      const scanned = res.headers.get("x-log-lines-scanned") ?? "?";
      setExportNote(
        `Saved ${name} — ${kept} lines kept of ${scanned} scanned`
        + (res.headers.get("x-log-truncated") === "1" ? ", oldest truncated" : ""),
      );
    } catch (e) {
      setExportNote(`Could not export the logs: ${
        e instanceof Error ? e.message : String(e)}`);
    } finally {
      setExporting(false);
    }
  }
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

      <div className="flex flex-wrap items-center gap-3">
        <Button variant="ghost" disabled={exporting} onClick={() => void downloadBundle()}>
          <Download size={12} className="mr-1 inline" aria-hidden />
          {exporting ? "Preparing…" : "Export logs"}
        </Button>
        <span className="text-[11px] text-ink-3">
          The last 5 days, with the polling noise and DEBUG lines stripped out.
        </span>
        {exportNote && (
          <span data-testid="log-export-note" className="text-[11px] text-ink-2">
            {exportNote}
          </span>
        )}
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
