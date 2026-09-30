import { useCallback, useState } from "react";
import { FlaskConical } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { Button } from "@/components/shared/Button";
import { EmptyState } from "@/components/shared/EmptyState";
import { Tooltip } from "@/components/shared/Tooltip";
import { formatUtcTime } from "@/components/shared/format";
import { usePoll } from "@/hooks/usePoll";
import { asArray, asObject } from "@/lib/asArray";
import { cn } from "@/lib/cn";
import { TrendPaRecordCard } from "./TrendPaRecordCard";

/**
 * The Trend PA engine (docs/todo/012): trend-following price action.
 *
 * H4 higher highs and higher lows (or lower and lower), London and New York
 * only, a pullback to an H1 swing level, an M15 engulfing or pin bar, stop
 * beyond the pullback, target 2R.
 *
 * The replay and the live record sit side by side and are never pooled. The
 * live switch asks before it turns on: it is the switch that lets this engine
 * place real orders, and nothing about it should happen by a stray click.
 */
interface Props {
  /** The settings the engines obey -- the peer's in Remote mode. */
  settings: Record<string, unknown>;
  onSaveSetting: (key: string, value: number) => void;
}

const LIVE_KEY = "tpa_live_execution";
/** The engine looks every minute; this long without a look is a stall. */
const STALL_S = 180;

const ago = (s: number): string =>
  s < 90 ? `${Math.max(0, Math.round(s))}s` : `${Math.round(s / 60)} min`;

/**
 * Whether the engine is alive, from the report alone. A quiet market and a
 * dead engine both mean "no signal"; this tells them apart. Null when the
 * node sent none of the fields (an older build), because a guess here would
 * read as fact.
 */
function liveness(d: Record<string, unknown>, remote: boolean, nowS: number):
  { text: string; ok: boolean } | null {
  if (d["generating_here"] === undefined) return null;
  const where = remote ? "the VPS" : "this machine";
  const reportAge = remote && typeof d["generated_at"] === "number" ? nowS - (d["generated_at"] as number) : 0;
  const stale = reportAge > STALL_S ? ` The VPS report is ${ago(reportAge)} old.` : "";
  if (!d["running"]) return { text: `Stopped on ${where}.${stale}`, ok: false };
  if (d["generating_here"] === false)
    return { text: `Running, but not analysing on ${where}: the other node does that.${stale}`, ok: true };
  const at = d["last_evaluated_at"];
  if (typeof at !== "number") return { text: `Running on ${where}, no analysis yet.${stale}`, ok: false };
  // Both stamps are the reporting node's own clock, so compare them to each other.
  const ref = typeof d["generated_at"] === "number" ? (d["generated_at"] as number) : nowS;
  const gap = ref - at;
  if (gap > STALL_S)
    return { text: `Not analysing: last checked ${ago(gap)} ago on ${where}.${stale}`, ok: false };
  return { text: `Analysing on ${where}. Last checked ${ago(gap)} ago.${stale}`, ok: !stale };
}

export function TrendPaSection({ settings, onSaveSetting }: Props) {
  const poll = usePoll<Record<string, unknown>>(
    "engines/trend-pa/report",
    useCallback(() => api.get<Record<string, unknown>>("/api/engines/trend-pa/report"), []),
    15_000,
  );
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const runBacktest = useCallback(async () => {
    setNote(null);
    try {
      const r = await api.post<Record<string, unknown>>("/api/engines/trend-pa/backtest");
      setNote(r["started"] ? `Backtest started on the ${r["where"] === "remote" ? "VPS" : "local node"}.`
        : String(r["error"] ?? "Not started."));
      await poll.refresh();
    } catch (e) {
      setNote(e instanceof ApiError ? e.message : String(e));
    }
  }, [poll]);

  if (!poll.data) {
    return <EmptyState title={poll.error ? "Could not load the Trend PA panel" : "Loading"}
      hint={poll.error?.message} />;
  }
  const d = poll.data;
  const ml = asObject(d["ml"]);
  const live = Boolean(Number(settings[LIVE_KEY] ?? 0));
  const running = Boolean(d["backtest_running"]);
  const remote = d["where"] === "remote";
  const btAt = d["backtest_at"] as number | null;
  const alive = liveness(d, remote, Date.now() / 1000);

  const toggleLive = () => {
    if (live) onSaveSetting(LIVE_KEY, 0);
    else setAsking(true);
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <span data-testid="tpa-where"
          className={cn("rounded px-1.5 py-0.5", remote ? "bg-remote/15 text-remote" : "bg-surface-2 text-ink-2")}>
          {remote ? "Showing the VPS" : "Showing this machine"}
        </span>
        <span className="text-ink-3">{String(d["status"] || "")}</span>
        <span className="ml-auto" />
        <Button variant="ghost" onClick={() => void runBacktest()}
          disabledReason={running ? "A backtest is already running." : null}>
          <FlaskConical size={13} />
          {running ? "Backtest running…" : "Run the backtest"}
        </Button>
      </div>
      {alive && (
        <p data-testid="tpa-alive" className={cn("text-[11px]", alive.ok ? "text-profit" : "text-warning")}>
          {alive.text}
        </p>
      )}
      {note && <p role="status" className="text-[11px] text-ink-2">{note}</p>}

      <p className="text-[11px] text-ink-3">
        Buys only in an H4 uptrend (higher highs and higher lows, close above the EMA50),
        sells only in a downtrend. London and New York, 08:00-21:00 UTC. Waits for price to
        come back to an H1 swing level, then enters on a closed M15 engulfing or pin bar.
        Stop beyond the pullback, target 2R.
      </p>

      <div className="grid gap-3 lg:grid-cols-2">
        <TrendPaRecordCard title="Backtest" testId="tpa-backtest" summary={d["backtest"]}
          hint={btAt ? `Replayed over the bridge's history, $0.30 a trade in costs. Run ${formatUtcTime(btAt)}.`
            : "Not run yet. It runs once by itself when the engine first starts."} />
        <TrendPaRecordCard title="Live (virtual)" testId="tpa-live" summary={d["live"]}
          hint="What the engine has done since it started, filled at the real ask or bid." />
      </div>

      <section data-testid="tpa-ml" className="rounded border border-line p-3 text-[11px]">
        <h4 className="text-xs font-semibold text-ink-1">Model</h4>
        <p className={ml["armed"] ? "text-profit" : "text-ink-2"}>
          {ml["armed"] ? "Armed: it can veto a live trade it expects to lose."
            : "Not armed: it scores nothing and vetoes nothing."}{" "}
          <span className="text-ink-3">{String(ml["why"] ?? "")}</span>
        </p>
        <p className="text-ink-3">
          Trained on {String(ml["n"] ?? 0)} closed trades. It arms only with at least{" "}
          {String(ml["min_samples"] ?? 60)} and an AUC of {String(ml["min_auc"] ?? 0.55)} on the
          newest quarter it was not trained on.
        </p>
      </section>

      <section className="rounded border border-warning/40 bg-warning/5 p-3 text-[11px]">
        <label className="flex items-center gap-2 text-ink-1">
          <Tooltip label="Lets this engine send its signals to the account as real orders. Asks before it turns on.">
            <input type="checkbox" checked={live} onChange={toggleLive} className="accent-accent"
              aria-label="Place real orders from this engine" />
          </Tooltip>
          Place real orders from this engine
        </label>
        <p className="mt-1 text-ink-3">
          Off by default. Test on the demo account first. Orders are refused until a strategy is
          chosen for "Trend PA Engine" on Trading &gt; Strategy, and every account limit (daily
          loss, daily goal, max open trades) still applies.
        </p>
        {asking && (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className="text-warning">This engine will open real positions on the account.</span>
            <Button variant="danger" onClick={() => { setAsking(false); onSaveSetting(LIVE_KEY, 1); }}>
              Yes, place real orders
            </Button>
            <Button variant="ghost" onClick={() => setAsking(false)}>Cancel</Button>
          </div>
        )}
      </section>

      <section className="rounded border border-line p-3">
        <h4 className="mb-1 text-xs font-semibold text-ink-1">Why it did not trade</h4>
        <ul data-testid="tpa-log" className="max-h-40 space-y-0.5 overflow-auto text-[10px]">
          {asArray<Record<string, unknown>>(d["log"]).map((r, i) => (
            <li key={i} className="flex gap-2">
              <span className="num text-ink-3">{formatUtcTime(r["ts"] as number)}</span>
              <span className="text-ink-2">{String(r["reason"])}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
