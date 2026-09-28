import { useCallback } from "react";
import { api } from "@/api/client";
import { usePoll } from "@/hooks/usePoll";
import { formatCompactMoney, formatPrice } from "@/components/shared/format";
import { DashCard } from "./DashCard";

export interface GexSnapshot {
  asof_date: string;
  taken_at: number | null;
  underlying: string;
  spot: number | null;
  xau_spot: number | null;
  ratio: number | null;
  total_gex: number | null;
  flip_level: number | null;
  call_wall: number | null;
  put_wall: number | null;
  xau_flip_level: number | null;
  xau_call_wall: number | null;
  xau_put_wall: number | null;
  n_rows: number | null;
  expiries: string[];
  source: string | null;
  assumptions: string | null;
}

export interface GexReport {
  snapshot: GexSnapshot | null;
  n_snapshots: number;
  target_snapshots: number;
  age_days: number | null;
  stale: boolean | null;
  regime: "negative" | "positive" | null;
  spot_vs_flip: "above" | "below" | null;
}

function Level({ id, label, xau, gld }: {
  id: string; label: string; xau: number | null; gld: number | null;
}) {
  return (
    <div className="min-w-0" data-testid={id}>
      <div className="text-[10.5px] uppercase tracking-wide text-ink-3">{label}</div>
      <div className="num text-sm text-ink-1">{formatPrice(xau)}</div>
      <div className="num text-[10.5px] text-ink-3">GLD {formatPrice(gld)}</div>
    </div>
  );
}

function regimeLine(r: GexReport): string {
  if (!r.regime) return "Regime unknown";
  const size = formatCompactMoney(r.snapshot?.total_gex);
  const where = r.spot_vs_flip ? ` · spot ${r.spot_vs_flip} the flip` : "";
  return `${r.regime === "negative" ? "Negative" : "Positive"} gamma · ${size} per 1%${where}`;
}

/**
 * The latest daily GLD option-chain snapshot (docs/todo/009): the GEX flip
 * and the call and put walls, converted to XAUUSD at the snapshot's ratio.
 * Display only. Nothing that trades reads these levels; the card is here so
 * the history can be watched building toward the study that decides whether
 * they help.
 */
export function GexView({ report }: { report: GexReport | null | undefined }) {
  if (!report) return <p className="text-xs text-ink-3">Loading GEX…</p>;
  const s = report.snapshot;
  if (!s) {
    return (
      <p className="text-xs text-ink-3">
        No GEX snapshot yet. One is taken each weekday from 22:00 London, where the
        Reversal engine is running.
      </p>
    );
  }
  return (
    <div className="space-y-2">
      <p data-testid="gex-regime"
        className={report.regime === "negative" ? "text-xs text-warning" : "text-xs text-ink-1"}>
        {regimeLine(report)}
      </p>
      <div className="grid grid-cols-3 gap-2">
        <Level id="gex-put-wall" label="Put wall" xau={s.xau_put_wall} gld={s.put_wall} />
        <Level id="gex-flip" label="Flip" xau={s.xau_flip_level} gld={s.flip_level} />
        <Level id="gex-call-wall" label="Call wall" xau={s.xau_call_wall} gld={s.call_wall} />
      </div>
      {report.stale && (
        <p data-testid="gex-stale" className="text-[11px] text-warning">
          Snapshot is {Math.round(report.age_days ?? 0)} days old: recent ones were missed.
        </p>
      )}
      <p data-testid="gex-history" className="text-[11px] text-ink-3">
        {s.asof_date} · gold {formatPrice(s.xau_spot)} · {report.n_snapshots} of{" "}
        {report.target_snapshots} snapshots for the study
      </p>
    </div>
  );
}

export function GexCard() {
  const poll = usePoll<GexReport>(
    "gex/latest",
    useCallback(() => api.get<GexReport>("/api/gex/latest"), []),
    300_000,
  );
  return (
    <DashCard title="GEX (GLD options)" icon="target" badge="daily · display only"
      footnote="Assumes dealers are long calls and short puts. GLD is a small slice of gold options. Nothing trades on this.">
      <div className="p-3">
        {poll.error && !poll.data
          ? <p className="text-xs text-loss">Could not read GEX.</p>
          : <GexView report={poll.data} />}
      </div>
    </DashCard>
  );
}
