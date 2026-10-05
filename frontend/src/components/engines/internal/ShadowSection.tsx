import { EmptyState } from "@/components/shared/EmptyState";
import { formatMoney, formatUtcTime, pnlColour } from "@/components/shared/format";
import { asArray } from "@/lib/asArray";
import { cn } from "@/lib/cn";

export interface ShadowRow {
  variant: string;
  is_champion: boolean;
  n_taken: number;
  n_skipped: number;
  net: number;
  mean_r: number | null;
  /** The dollar result of the signals this variant skipped: a skip on a loser
   *  is the variant being right, and "taken" alone cannot show it. */
  avoided_net?: number;
  /** R from the dollars the whole trade banked, not the last leg's points. */
  mean_r_full?: number | null;
  mean_net?: number | null;
  /** Net minus the live variant's net, over the same signals. */
  delta_vs_champion?: number;
}

export interface LedgerCall {
  take: boolean;
  reason: string;
}

/** One signal, its result stated ONCE, and every variant's call beside it. */
export interface LedgerRow {
  signal_ref: string;
  ts: number;
  direction: string | null;
  status: string | null;
  outcome: string | null;
  net: number | null;
  r_full: number | null;
  r_replay: number | null;
  /** The broker placed it, so its dollars are at the real lot and no R can be
   *  derived from them. */
  executed: boolean;
  calls: Record<string, LedgerCall>;
}

export interface HistoryRow {
  ts: number;
  signal_ref: string;
  variant: string;
  would_take: number;
  reason: string | null;
  direction: string | null;
  status: string | null;
  outcome: string | null;
  net: number | null;
  r: number | null;
}

/**
 * The virtual trades: what each configuration would have done.
 *
 * The champion is what is live. A challenger is a configuration being measured
 * against it on the same signals, without any money — which is the only honest
 * way to find out whether a change is an improvement before shipping it.
 *
 * **A skip is a row, not an absence.** A variant that skipped a losing trade
 * and one that never saw it both contribute nothing to the P&L, and only one
 * of them is evidence. The skipped rows carry the outcome they avoided.
 *
 * `mean_r` and `r` are blank, never 0.000, where there is nothing to score.
 * Zero expectancy and no evidence are different statements, and a table that
 * renders both as 0.000 invites the wrong one to be acted on — the rule
 * `shadow.report()` states in its own docstring.
 */
export function ShadowSection({ shadow, history, ledger, realised, edge }: {
  shadow: unknown; history: unknown;
  /** One row per signal. When present it replaces the flat decision history,
   *  which repeated one trade's dollars once per variant (owner, 2026-10-02). */
  ledger?: unknown;
  realised: Record<string, unknown>;
  edge?: Record<string, unknown>;
}) {
  const rows = asArray<ShadowRow>(shadow);
  const decisions = asArray<HistoryRow>(history);
  const ledgerRows = asArray<LedgerRow>(ledger);
  const variantNames = rows.length > 0
    ? rows.map((r) => r.variant)
    : Array.from(new Set(ledgerRows.flatMap((l) => Object.keys(l.calls))));
  // `{n, total, per_trade}` -- the shape `panel_data.get_realised_pnl` really
  // returns. The first version of this read `net_pnl`, a key that does not
  // exist, so the line was silently absent: the same mistake this panel's own
  // pro-model section had been making, made again two hundred lines away.
  // Checked against the live payload, 2026-09-19.
  const net = typeof realised?.["total"] === "number"
    ? (realised["total"] as number) : null;
  const n = typeof realised?.["n"] === "number" ? (realised["n"] as number) : null;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className="text-xs font-semibold text-ink-1">Virtual trades</h3>
        {net != null && (
          <span data-testid="shadow-realised" className="text-[11px] text-ink-3">
            the engine's real closed P&amp;L is{" "}
            <span className={cn("num font-semibold", pnlColour(net))}>
              {formatMoney(net)}
            </span>
            {/* -$206 over 58 trades and -$206 over 3 are different
                statements, and only one of them is a verdict. */}
            {n != null && <span className="num"> over {n}</span>}
          </span>
        )}
        {/* The pair the NiceGUI Edge tab was built around, which this engine
            had nowhere. A win rate alone decides nothing: 38% with an average
            win three times the average loss is profitable, and 60% with the
            ratio inverted is not. */}
        {edge && typeof edge["expectancy"] === "number" && (
          <span data-testid="shadow-edge" className="text-[11px] text-ink-3">
            {" · "}PF{" "}
            <span className={cn("num font-semibold",
              typeof edge["profit_factor"] !== "number" ? "text-ink-3"
                : (edge["profit_factor"] as number) >= 1 ? "text-profit" : "text-loss")}>
              {/* Null, not 0: an engine that has never lost has no ratio yet,
                  and 0.00 reads as the worst possible one. */}
              {typeof edge["profit_factor"] === "number"
                ? (edge["profit_factor"] as number).toFixed(2) : "not yet"}
            </span>
            {", expectancy "}
            <span className={cn("num font-semibold",
              pnlColour(edge["expectancy"] as number))}>
              {formatMoney(edge["expectancy"] as number)}
            </span>
            <span> per trade</span>
          </span>
        )}
      </div>

      {rows.length === 0 ? (
        <EmptyState
          title="No variant has seen a closed signal yet"
          hint="A challenger is scored only on signals that have since closed."
        />
      ) : (
        <table data-testid="shadow-table" className="w-full text-left text-[11px]">
          <thead className="text-ink-3">
            <tr>
              {["Variant", "Taken", "Skipped", "Net", "Avoided", "vs live", "Mean R", "Mean R (whole)"].map((h) => (
                <th key={h} className="px-2 py-1 font-normal">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="num">
            {rows.map((r) => (
              <tr key={r.variant} data-testid={`variant-${r.variant}`}
                className="border-t border-line">
                <td className="px-2 py-1 text-ink-1">
                  {r.variant}
                  {r.is_champion && (
                    <span className="ml-1.5 rounded bg-accent/15 px-1 py-0.5 text-[9px] text-accent">
                      live
                    </span>
                  )}
                </td>
                <td className="px-2 py-1 text-ink-2">{r.n_taken}</td>
                <td className="px-2 py-1 text-ink-3">{r.n_skipped}</td>
                <td className={cn("px-2 py-1", pnlColour(r.net))}>{formatMoney(r.net)}</td>
                <td className={cn("px-2 py-1",
                  r.avoided_net == null ? "text-ink-3" : pnlColour(r.avoided_net))}>
                  {r.avoided_net == null ? "—" : formatMoney(r.avoided_net)}
                </td>
                <td className={cn("px-2 py-1",
                  r.delta_vs_champion == null ? "text-ink-3" : pnlColour(r.delta_vs_champion))}>
                  {r.is_champion || r.delta_vs_champion == null
                    ? "—" : formatMoney(r.delta_vs_champion)}
                </td>
                <td className="px-2 py-1 text-ink-2">
                  {r.mean_r == null ? "—" : r.mean_r.toFixed(3)}
                </td>
                <td className="px-2 py-1 text-ink-2">
                  {r.mean_r_full == null ? "—" : r.mean_r_full.toFixed(3)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {ledgerRows.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-ink-1">Ledger</h4>
          <p className="mb-1 text-[10px] text-ink-3">
            One row per signal. Its result is stated once; each variant's call
            sits beside it. A skip on a loser is the variant being right.
          </p>
          <div className="max-h-72 overflow-auto">
            <table data-testid="ledger-table" className="w-full text-left text-[11px]">
              <thead className="text-ink-3">
                <tr>
                  {["When", "Signal", "Result", "R (whole)", "R (replay)", ...variantNames]
                    .map((h) => <th key={h} className="px-2 py-1 font-normal">{h}</th>)}
                </tr>
              </thead>
              <tbody className="num">
                {ledgerRows.map((l) => (
                  <tr key={l.signal_ref} data-testid={`ledger-${l.signal_ref}`}
                    className="border-t border-line">
                    <td className="px-2 py-1 text-ink-3">{formatUtcTime(l.ts)}</td>
                    <td className="px-2 py-1 text-ink-3">
                      {l.direction ?? "—"} · {l.status ?? "—"}
                    </td>
                    <td className={cn("px-2 py-1", l.net == null ? "text-ink-3" : pnlColour(l.net))}>
                      {l.net == null ? "—" : formatMoney(l.net)}
                    </td>
                    <td className="px-2 py-1 text-ink-2">
                      {/* A broker trade's dollars are at the real lot, so no R
                          can be derived: say so rather than show a wrong one. */}
                      {l.executed ? "broker"
                        : l.r_full == null ? "—" : l.r_full.toFixed(2)}
                    </td>
                    <td className="px-2 py-1 text-ink-2">
                      {l.r_replay == null ? "—" : l.r_replay.toFixed(2)}
                    </td>
                    {variantNames.map((v) => {
                      const c = l.calls[v];
                      return c ? (
                        <td key={v} data-testid={`call-${l.signal_ref}-${v}`}
                          title={c.reason || undefined}
                          className={cn("px-2 py-1", c.take ? "text-profit" : "text-ink-3")}>
                          {c.take ? "took" : "skipped"}
                        </td>
                      ) : <td key={v} className="px-2 py-1 text-ink-3">—</td>;
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {ledgerRows.length === 0 && (
      <div>
        <h4 className="text-xs font-semibold text-ink-1">Decision history</h4>
        <p className="mb-1 text-[10px] text-ink-3">
          Every variant's call on every signal, newest first. A skip on a loser
          is the variant being right.
        </p>
        {decisions.length === 0 ? (
          <EmptyState title="No decisions recorded yet" />
        ) : (
          <div className="max-h-72 overflow-auto">
            <table data-testid="shadow-history" className="w-full text-left text-[11px]">
              <thead className="text-ink-3">
                <tr>
                  {["When", "Variant", "Call", "Signal", "R", "Would have made", "Why"]
                    .map((h) => <th key={h} className="px-2 py-1 font-normal">{h}</th>)}
                </tr>
              </thead>
              <tbody className="num">
                {decisions.map((d, i) => {
                  const took = Boolean(d.would_take);
                  return (
                    <tr key={`${d.signal_ref}-${d.variant}-${i}`}
                      data-testid={`decision-${d.signal_ref}-${d.variant}`}
                      className="border-t border-line">
                      <td className="px-2 py-1 text-ink-3">{formatUtcTime(d.ts)}</td>
                      <td className="px-2 py-1 text-ink-2">{d.variant}</td>
                      <td className={cn("px-2 py-1", took ? "text-profit" : "text-ink-3")}>
                        {took ? "took" : "skipped"}
                      </td>
                      <td className="px-2 py-1 text-ink-3">
                        {d.direction ?? "—"} · {d.status ?? "—"}
                      </td>
                      <td className="px-2 py-1 text-ink-2">
                        {d.r == null ? "—" : d.r.toFixed(2)}
                      </td>
                      <td className={cn("px-2 py-1",
                        d.net == null ? "text-ink-3" : pnlColour(took ? d.net : -d.net))}>
                        {d.net == null ? "—" : formatMoney(d.net)}
                      </td>
                      <td className="px-2 py-1 text-ink-3">{d.reason || "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
      )}
    </div>
  );
}
