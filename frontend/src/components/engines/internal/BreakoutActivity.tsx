import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { formatBrokerTime, formatMoney, pnlColour } from "@/components/shared/format";
import { asArray, asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";

/**
 * What the Breakout engine has been doing, and why it has not.
 *
 * Three of `panel_data`'s seventeen operations, and the last with no caller.
 * The analysis log is the one that earns its place: every other panel in this
 * app reports what an engine DID, and a suppressed candidate leaves no trade
 * behind to look at — so without it there is no way to tell a quiet market
 * from a gate set too tight.
 *
 * Collapsed by default. This is detail for a question, not a dashboard.
 */
function num(value: unknown): number | null {
  if (value == null || value === "") return null;
  const v = typeof value === "number" ? value : Number(value);
  return Number.isFinite(v) ? v : null;
}

function Panel({ title, count, children, testId }: {
  title: string; count: number; children: React.ReactNode; testId: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded border border-line">
      <button
        type="button"
        data-testid={`${testId}-toggle`}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left text-[11px] text-ink-2 hover:bg-surface-2"
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span className="font-semibold text-ink-1">{title}</span>
        <span className="num ml-auto text-ink-3">{count}</span>
      </button>
      {open && (
        <div data-testid={testId} className="max-h-72 overflow-auto border-t border-line p-2">
          {count === 0
            ? <p className="text-[11px] text-ink-3">nothing recorded yet</p>
            : children}
        </div>
      )}
    </div>
  );
}

export function BreakoutActivity({ signals, log, params }: {
  signals: unknown; log: unknown; params: unknown;
}) {
  const rows = asArray<Record<string, unknown>>(signals);
  const entries = asArray<Record<string, unknown>>(log);
  const tuned = asObject(params);
  const tunedKeys = Object.keys(tuned);

  return (
    <div className="grid gap-2 lg:grid-cols-3">
      <Panel title="Recent signals" count={rows.length} testId="bo-signals">
        <table className="w-full text-left text-[11px]">
          <tbody className="num">
            {rows.map((r, i) => {
              const pnl = num(r["net_pnl_dollars"] ?? r["pnl_dollars"]);
              return (
                <tr key={String(r["signal_ref"] ?? r["id"] ?? i)}
                  data-testid={`bo-signal-${r["id"] ?? i}`}
                  className="border-t border-line">
                  <td className="py-1 text-ink-3">
                    {formatBrokerTime(num(r["created_at"]))}
                  </td>
                  <td className={cn("py-1",
                    r["direction"] === "BUY" ? "text-profit" : "text-loss")}>
                    {String(r["direction"] ?? "—")}
                  </td>
                  <td className="py-1 text-ink-3">{String(r["breakout_type"] ?? "—")}</td>
                  <td className="py-1 text-ink-3">{String(r["outcome"] ?? r["status"] ?? "—")}</td>
                  <td className={cn("py-1 text-right", pnlColour(pnl ?? 0))}>
                    {pnl == null ? "—" : formatMoney(pnl)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Panel>

      <Panel title="Why it acted, or did not" count={entries.length} testId="bo-log">
        <ul className="space-y-1">
          {entries.map((e, i) => (
            <li key={String(e["id"] ?? i)} data-testid={`bo-log-${e["id"] ?? i}`}
              className="flex gap-2 border-t border-line pt-1 first:border-0 first:pt-0">
              <span className="num shrink-0 text-[10px] text-ink-3">
                {formatBrokerTime(num(e["ts"]))}
              </span>
              <span className="text-[11px] text-ink-2">
                {/* The reason, not the verdict. "suppressed" on its own is
                    the thing this panel exists to replace. */}
                {String(e["suppressed_reason"] || e["claude_decision"]
                  || e["result"] || "—")}
              </span>
            </li>
          ))}
        </ul>
      </Panel>

      <Panel title="Self-tuned parameters" count={tunedKeys.length} testId="bo-params">
        <table className="w-full text-left text-[11px]">
          <tbody>
            {tunedKeys.map((key) => {
              const p = asObject(tuned[key]);
              const value = num(p["value"]);
              const dflt = num(p["default"]);
              // A threshold that has drifted from its default is the engine
              // telling you something. A number with nothing to compare it
              // to is not.
              const drifted = value != null && dflt != null && value !== dflt;
              return (
                <tr key={key} data-testid={`bo-param-${key}`} className="border-t border-line">
                  <td className="py-1 text-ink-2" title={String(p["desc"] ?? "")}>{key}</td>
                  <td className={cn("num py-1 text-right",
                    drifted ? "font-semibold text-accent" : "text-ink-1")}>
                    {value == null ? "—" : value}
                  </td>
                  <td className="num py-1 text-right text-ink-3">
                    {dflt == null ? "" : drifted ? `was ${dflt}` : "default"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}
