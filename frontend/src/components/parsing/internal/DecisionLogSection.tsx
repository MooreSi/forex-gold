import { useCallback, useEffect, useState } from "react";
import { FlaskConical } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { asArray, asObject } from "@/lib/asArray";

interface Summary {
  total: number; executed: number; blocked: number;
  resolved: number; awaiting_outcome: number;
  observed: number; reconstructed: number;
  by_path: { path: string; executed: number; blocked: number }[];
  top_reasons: { reason: string; n: number }[];
}

interface Variant {
  variant: string; is_champion: boolean;
  n_taken: number; n_skipped: number; n_abstained: number;
  /** null, never 0, for a variant that has scored nothing. */
  mean_r: number | null;
  net: number;
}

/**
 * What the app decided about each Telegram signal, and what four gates that
 * are currently OFF would have decided.
 *
 * **Recording only — nothing here changes a trade.** The switch that turns the
 * recording on is a parsing toggle on the Settings sub-tab, default off.
 *
 * Two readouts, because they become useful at different times. The summary is
 * worth reading from the first decision; champion-vs-challenger needs trades
 * that have closed, so it says nothing for days and is the reason the whole
 * thing exists.
 *
 * Neither is polled. This sits behind a live trading page, and a card that
 * polls a database every few seconds to show a number that moves twice a day
 * is a cost with no benefit — so the summary loads once and the rest is on a
 * button.
 */
export function DecisionLogSection() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [variants, setVariants] = useState<Variant[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const loadSummary = useCallback(async () => {
    try {
      setSummary(asObject<Summary>(await api.get<Summary>("/api/decision-log/summary")));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, []);

  useEffect(() => { void loadSummary(); }, [loadSummary]);

  async function loadReport() {
    setBusy(true);
    try {
      const res = await api.get<{ variants: Variant[] }>("/api/decision-log/report");
      setVariants(asArray<Variant>(asObject<{ variants: Variant[] }>(res).variants));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally { setBusy(false); }
  }

  async function backfill() {
    setBusy(true);
    setNote(null);
    try {
      const res = await api.post<{ added: number; note: string }>(
        "/api/decision-log/backfill", {});
      setNote(res.note);
      await loadSummary();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally { setBusy(false); }
  }

  return (
    <section data-testid="decision-log" className="rounded border border-line p-3">
      <h3 className="flex items-center gap-2 text-xs font-semibold text-ink-1">
        <FlaskConical size={13} className="text-remote" />
        Signal Decision Log
      </h3>
      <p className="mb-3 text-[11px] text-ink-3">
        What the app decided about each Telegram signal, and what four gates that
        are currently OFF would have decided. Recording only — nothing here
        changes a trade.
      </p>

      {error && <p role="alert" className="mb-2 text-xs text-loss">{error}</p>}

      {summary && summary.total === 0 ? (
        <p className="text-[11px] text-ink-3">
          Nothing recorded yet. Switch the log on under Settings and it fills as
          signals arrive — or rebuild it from past trades below.
        </p>
      ) : summary ? (
        <div className="space-y-0.5 font-mono text-[11px] text-ink-2">
          <p className="text-ink-1">
            {summary.total} decisions — {summary.executed} executed,{" "}
            {summary.blocked} declined
          </p>
          <p className="text-ink-3">
            {summary.resolved} scored, {summary.awaiting_outcome} still open ·{" "}
            {summary.observed} observed, {summary.reconstructed} rebuilt from past trades
          </p>
          {asArray<Summary["by_path"][number]>(summary.by_path).map((row) => (
            <p key={row.path} className="text-ink-3">
              {row.path === "ime" ? "Immediate Market" : "Full signal"}:{" "}
              {row.executed} executed, {row.blocked} declined
            </p>
          ))}
          {asArray<Summary["top_reasons"][number]>(summary.top_reasons).length > 0 && (
            <>
              <p className="pt-2 text-[10px] font-semibold uppercase tracking-wider text-ink-3">
                Why they were declined
              </p>
              {asArray<Summary["top_reasons"][number]>(summary.top_reasons).map((row) => (
                <p key={row.reason} className="text-ink-3">{row.n}x  {row.reason}</p>
              ))}
            </>
          )}
        </div>
      ) : null}

      {variants && (
        <div className="mt-3 space-y-0.5 font-mono text-[11px] text-ink-2">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-ink-3">
            Champion vs challenger
          </p>
          {[...variants].sort((a, b) => Number(b.is_champion) - Number(a.is_champion))
            .map((v) => (
              <p key={v.variant} className="text-ink-3">
                {v.variant}{v.is_champion ? " (live)" : ""}: took {v.n_taken},
                {" "}stood aside {v.n_skipped}
                {v.n_abstained ? `, ${v.n_abstained} no opinion` : ""},{" "}
                {/* null, not 0. No evidence and flat expectancy are different
                    statements, and rendering both as 0.000 invites the wrong
                    one to be acted on. */}
                {v.mean_r === null || v.mean_r === undefined
                  ? "no decisions yet"
                  : `${v.mean_r >= 0 ? "+" : ""}${v.mean_r.toFixed(3)}R`},{" "}
                net {v.net >= 0 ? "+" : ""}{v.net.toFixed(2)}
              </p>
            ))}
        </div>
      )}

      {note && <p role="status" className="mt-2 text-[11px] text-profit">{note}</p>}

      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="ghost" disabled={busy} onClick={() => void loadSummary()}>
          Refresh
        </Button>
        <Button variant="ghost" disabled={busy} onClick={() => void loadReport()}>
          Champion vs challenger
        </Button>
        <Button
          variant="ghost"
          disabled={busy}
          onClick={() => void backfill()}
          title={
            "Reconstructs a decision for every past Telegram trade. The clock " +
            "gates and the entry trigger are rebuilt exactly; the news calendar " +
            "and the trend read are gone and record no opinion. Rebuilt rows are " +
            "marked as such. Safe to press twice."
          }
        >
          Rebuild from past trades
        </Button>
      </div>
    </section>
  );
}
